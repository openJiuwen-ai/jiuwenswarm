"""Record the latest plan check separately from historical performance samples."""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import subprocess
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PREVIOUS = ROOT / "artifacts/4689/review-20261011"
ISSUE = "https://gitcode.com/openJiuwen/jiuwenswarm/issues/4689"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, encoding="utf-8").strip()


def text_log(name):
    raw = (PREVIOUS / name).read_bytes()
    text = raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8")
    return text.replace("\r\n", "\n")


def link(path, label=None):
    target = str(path).replace("\\", "/")
    return f'<a href="{html.escape(target)}">{html.escape(label or target)}</a>'


def main():
    previous = json.loads((PREVIOUS / "review-results.json").read_text(encoding="utf-8"))
    code = git("log", "-1", "--format=%H", "--", "jiuwenswarm", "scripts", "tests")
    assert code.startswith("551ac7a11"), code
    logs = [
        ("PersonalContext / Kernel / 模型更新 / RunContext", "plan-recheck-focused.log"),
        ("完整 Process CLI / Python SDK / RunContext", "plan-recheck-process-cli.log"),
        ("Worker / Front 真进程导入边界 / 关闭 / 桌面入口契约", "plan-recheck-worker-front-final.log"),
        ("外部真实模型 SDK 续聊及真实 SDK LSP", "plan-recheck-real-sdk.log"),
    ]
    runs = []
    for label, name in logs:
        content = text_log(name)
        summary = re.findall(r"^=+ (.*?\d+ passed.*?) =+$", content, re.M)
        assert summary and not re.search(r"^FAILED ", content, re.M), name
        runs.append({"suite": label, "log": name + ".gz", "summary": summary[-1], "code_commit": code})
    for name in [name for _, name in logs] + ["plan-recheck-before-fix.log", "plan-recheck-worker-front.log", "plan-recheck-native-tool-driver.log", "plan-recheck-native-tool.log"]:
        raw = (PREVIOUS / name).read_bytes()
        (OUT / (name + ".gz")).write_bytes(gzip.compress(raw, mtime=0))
    native = json.loads((PREVIOUS / "plan-recheck-native-tool.json").read_text(encoding="utf-8"))
    assert native["same_worker"] and native["same_instance"] and native["same_execution"]
    assert native["side_effects"] == ["once"] and native["shutdown"]["ok"]
    (OUT / "native-tool.json").write_text(json.dumps(native, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with urllib.request.urlopen("https://api.gitcode.com/api/v5/repos/openJiuwen/jiuwenswarm/issues/4689", timeout=20) as response:
        issue = json.load(response)
    (OUT / "issue4689-original.json").write_text(json.dumps({"url": ISSUE, **{k: issue.get(k) for k in ("title", "body", "updated_at")}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    unchanged = ["pyproject.toml", "uv.lock", "jiuwenswarm/channels/web/frontend/package.json", "jiuwenswarm/channels/web/frontend/package-lock.json"]
    assert not git("diff", "--name-only", previous["baseline"], "HEAD", "--", *unchanged, "jiuwenswarm/channels/tui")
    subprocess.run(["git", "diff", "--check", previous["baseline"], "HEAD"], cwd=ROOT, check=True)
    product = git("diff", "--name-only", "124b97684", "HEAD", "--", "jiuwenswarm", "scripts").splitlines()
    assert product == ["jiuwenswarm/server/runtime/agent_adapter/interface_deep.py"], product
    requirements = previous["requirements"]
    phases = [
        ["阶段二", "仓内功能已实现并回归", "Worker 持有执行，断连保留；序号回放与待答交互恢复；实际交付 ACK；独立 dispatch_id 与调用方去重；metadata 合并；目录/profile 隔离；统一关闭期限。", "本轮另补齐 PersonalContext Rail 的可信数据目录，使其与 Host 使用同一目录。Web 的浏览器点击验收仍未实测。"],
        ["阶段三", "共享资源和职责拆分已实现并回归", "Kernel 及租约共享模型、工具工厂、Rail 配方、Skill 索引、MCP 和项目 LSP；RunContext 保存本次执行选项；状态型 Rail/工具/模型组路由保留 Agent 所有权。", "SessionExecution 复用既有 RuntimeSessionCoordinator 与 handles，历史/checkpoint 保留会话归属；未新增同名总类。本轮补齐完整 Kernel 身份变化后的模型缓存失效。"],
        ["阶段四", "常驻与启动路径改造已实现；验收未全部完成", "Front 轻量控制面、优先连接旧 Worker；按依赖初始化；可选能力后台/首次使用；模型/MCP/LSP 复用，沿用 KVCache 进程所有权；Windows 与冻结入口适配。", "冷 Worker 指标未达标；冻结 EXE、旧协议安装升级未做真实端到端验证；可选能力的全部开关/故障组合未逐一验收。"],
    ]
    metrics = previous["performance_provenance"]
    goals = [
        ["1. Front 开始监听", "P95 ≤300 ms", "agent 259.15 ms；code.normal 259.95 ms", "此前正式采样达标；本轮未重采 P95"],
        ["2. 控制面可用", "P95 ≤500 ms", "四类查询最大 P95：agent 368.27 ms；code.normal 373.60 ms", "此前正式采样达标；本轮真实 Front 控制查询回归通过"],
        ["3. Gateway 不生成替代业务响应", "真实状态/明确错误；业务来自 AgentServer", "权威执行状态转发及交付 ACK；实际 Gateway 重启接回通过", "功能覆盖"],
        ["4. Front 无重依赖", "真实子进程模块检查；warming 控制面可用", "本轮 test_front_import_boundary.py 真进程检查通过", "功能覆盖"],
        ["5. Worker 常驻、重启接回", "PID / 实例 / 原执行一致；Runtime 不重载", f"本轮 Worker PID {native['worker_pid']}；实例与执行 ID 一致；工具 once；最终关闭成功", "真进程通过"],
        ["6. chat 进入真实执行", "已有 Worker P95 ≤500 ms；冷 Worker ≤2.5 s", "warm：158.96 / 160.00 ms；cold：8010.84 / 7989.27 ms（agent / code.normal）", "已有 Worker 的此前采样达标；冷 Worker 未达标"],
    ]
    findings = [
        ["阶段二", "PersonalContext Host 已改为可信目录，Rail 仍使用 Path.home()", "Rail 改用 get_user_workspace_dir()；独立目录回归通过", "../../../jiuwenswarm/server/runtime/agent_adapter/interface_deep.py"],
        ["阶段三", "Kernel 完整身份变化后，模型指纹相同仍可能命中旧缓存", "缓存命中同时校验 Kernel 身份；新执行获取新租约，原租约仍可使用", "../../../tests/unit_tests/agentserver/test_adapter_kernel_invalidation.py"],
        ["验收测试", "真实退出已由原生句柄确认，PID-only 查询仍可能显示存在", "去掉 PID-only 断言，保留关闭预算及原生子进程 wait 退出断言；修正前失败日志保留", "../../../tests/unit_tests/agentserver/test_worker_shutdown_deadline.py"],
    ]
    result = {
        "generated_at": datetime.now().astimezone().isoformat(), "issue": ISSUE,
        "baseline": previous["baseline"], "code_commit": code, "branch": git("branch", "--show-current"),
        "conclusion": "阶段二至四仓内改造按改进方案主线实施；不能宣称所有性能和端到端验收全部完成。",
        "phases": phases, "issue_goals": goals, "requirements": requirements,
        "new_findings_and_repairs": findings, "current_test_runs": runs,
        "native_worker_pid": native["worker_pid"], "native_side_effects": native["side_effects"],
        "dependency_files_unchanged": unchanged, "remote_tui_unchanged": True,
        "new_product_files": product, "performance_provenance": metrics,
        "previous_regression_code": previous["reviewed_code_commit"],
        "previous_regression_candidate_only_failures": previous["candidate_only_failures"],
        "previous_broad_unique_failures": previous["current_unique_failures"],
        "limitations": previous["limitations"],
    }
    (OUT / "completion-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def table(headers, rows):
        return '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + html.escape(v) + '</th>' for v in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'

    tests_table = '<table><tr><th>本轮测试</th><th>结果</th><th>原始日志</th></tr>' + ''.join('<tr><td>' + html.escape(r['suite']) + '</td><td>' + html.escape(r['summary']) + '</td><td>' + link(r['log'], 'gzip 原始日志') + '</td></tr>' for r in runs) + '</table>'
    fix_table = '<table><tr><th>归属</th><th>发现</th><th>修正及证据</th></tr>' + ''.join('<tr><td>' + html.escape(r[0]) + '</td><td>' + html.escape(r[1]) + '</td><td>' + html.escape(r[2]) + '<br>' + link(r[3], '源文件') + '</td></tr>' for r in findings) + '</table>'
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Issue #4689 阶段二三四完成情况复核</title><style>
    body{margin:0;background:#eef2f7;color:#24334b;font:16px/1.65 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1120px;margin:32px auto;padding:24px 32px;background:#fff;border-radius:12px}h1{font-size:28px}h2{margin-top:32px;font-size:21px}p{margin:12px 0}a{color:#1765bd;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:12px;border:1px solid #d9e2ec;text-align:left;vertical-align:top}th{background:#eef4fb}.note{padding:18px;background:#fff6df;border-left:4px solid #c99423}.scroll{overflow:auto}code{overflow-wrap:anywhere}@media(max-width:700px){main{margin:0;padding:18px}h1{font-size:24px}table{min-width:600px}}</style><main>'''
    page += '<h1>Issue #4689 · 阶段二、三、四完成情况复核</h1>'
    page += '<p>复核日期：2026-10-12。' + link(ISSUE, '原 Issue') + '；原文已通过 GitCode API 重新读取，并保存 ' + link('issue4689-original.json', '原文快照') + '。</p>'
    page += '<p class="note">阶段二至四的仓内改造已经按改进方案主线实施。本次又发现并修正两处计划内遗漏。<strong>仍不能宣称所有验收全部完成</strong>：冷 Worker 未达到 2.5 秒，浏览器交互、冻结 EXE 和旧协议安装升级等路径尚缺真实端到端验收。依照用户要求保持现有依赖，不扩展到 agent-core，也不修复无关基线问题。</p>'
    page += '<p>develop 基线：<code>' + result['baseline'] + '</code><br>当前产品代码提交：<code>' + code + '</code><br>独立分支：<code>' + result['branch'] + '</code></p>'
    page += '<h2>逐阶段结论</h2>' + table(['阶段', '结论', '已经落地的主线', '实现方式及验收边界'], phases)
    page += '<h2>本轮发现与补齐</h2>' + fix_table
    page += '<p>本轮产品代码只修改 interface_deep.py 的两处行为；其余是回归测试和验收附件。依赖文件、远程 TUI /tui 通路均未修改，原工作区保持原提交和原有文件。</p>'
    page += '<h2>Issue 六项目标</h2><p>数值证据来自此前 Windows 源码正式采样：每种工作模式、每版本 30 次 Front 冷启动、30 次冷 Worker 请求及 100 次已有 Worker 请求。计时结束点为必需执行准备完成后的实际 SDK 执行入口。<strong>采样代码为 ' + metrics['code_commit'][:12] + '，本轮未对 ' + code[:12] + ' 重新采集 P95。</strong></p>' + table(['目标', '要求', '证据', '状态'], goals)
    page += '<h2>本轮回归与真实运行</h2>' + tests_table
    page += '<p>真实 Gateway/Front 重启验证：' + link('native-tool.json', '完整事件与历史证据') + '。Worker、实例、执行 ID 一致，PowerShell 工具副作用为 once；最终 shutdown.done.ok 为 true。该重启场景使用真实 SDK 和工具子进程、本地模型协议 fixture；外部真实模型续聊由上表独立测试覆盖。</p>'
    page += '<p>首次新增两条遗漏回归在修正前均失败，' + link('plan-recheck-before-fix.log.gz', '修正前日志') + '。Worker/Front 首轮为 73 passed、1 skipped、1 failed，失败为上述 PID-only 断言；' + link('plan-recheck-worker-front.log.gz', '初轮日志') + '已保留，修正后 74 passed、1 skipped。</p>'
    page += '<p>上一轮全面回归属于 b126a8d10：Process CLI 805、Runtime/Server 7415、Gateway/AgentOS/Desktop 1850、Agent/Rail 等 926 项通过，Web 46 项通过并完成生产构建。该轮 87 项失败均属于已经在原 develop 复现的失败集合；本轮未重新跑完整的上述大套件，不能把旧结果标成当前提交的新测试。详见 ' + link('../review-20261011/issue4689-scope-and-regression-review.html', '上一轮范围和回归报告') + '。</p>'
    page += '<h2>原方案与改进方案的对应方式</h2><p>以用户确认的改进方案为实施口径：保留状态型 Rail/工具的 Agent 所有权和真实 session claim/pin；复用已有会话协调器承担 SessionExecution 职责；Kernel 准备逐步替代虚拟会话预热。初始化先完成配置迁移和模型身份/路由依赖，再并行独立步骤；默认不新增多 Worker 调度和 WarmSpare。接口及身份隔离契约继续保留，Process CLI 仍是本地子进程 Runtime，远程 TUI 仍走 Gateway /tui。</p>'
    page += table(['阶段', '改进方案要求', '实现对应', '回归证据及边界'], requirements)
    page += '<h2>尚未达到及尚未完成验证</h2><p>冷 Worker P95 约 8 秒，未达到 2.5 秒；按用户边界保留现有 SDK 和锁定依赖。Web 浏览器页面操作、实际冻结 EXE、旧协议安装升级，以及全部可选能力开启/故障组合未完整实测。旧协议或代码身份不符时会明确拒绝接回；不自动迁移或重放未确定的工具执行。这些限制没有因本轮功能测试通过而消失。</p>'
    page += '<p>' + link('completion-check.json', '机器可读复核结果') + '</p></main></html>'
    (OUT / 'issue4689-completion-check.html').write_text(page, encoding='utf-8')
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []
        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                self.paths.extend(v for k, v in attrs if k == 'href' and not v.startswith('https://'))
    parser = Links()
    parser.feed(page)
    missing = [p for p in parser.paths if not (OUT / p).resolve().exists()]
    assert not missing, missing
    assert '�' not in page and '</html>' in page
    (OUT / 'report-validation.json').write_text(json.dumps({'code_commit': code, 'relative_links': len(parser.paths), 'missing': missing, 'html_sha256': hashlib.sha256(page.encode()).hexdigest()}, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'code_commit': code, 'test_runs': runs, 'missing_links': missing}, ensure_ascii=False))


if __name__ == '__main__':
    main()
