"""Render remaining acceptance from current raw process measurements and evidence."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def main(args):
    root = Path(__file__).resolve().parents[2]
    out = Path(args.directory).resolve()
    metrics = {}
    for mode, suffix in [("agent", "agent"), ("code.normal", "code")]:
        front = [json.loads(line) for line in (out / f"front-{suffix}.jsonl").read_text().splitlines()]
        chat = [json.loads(line) for line in (out / f"chat-{suffix}.jsonl").read_text().splitlines()]
        metrics[mode] = {}
        for label in ["baseline", "candidate"]:
            rows = [r for r in front if r["label"] == label]
            assert len(rows) == 30
            values = {"listen": [r["listen_ms"] for r in rows]}
            for name in ["session.list", "project.list", "config.get", "history.get"]:
                values[name] = [r["controls"][name]["available_from_spawn_ms"] for r in rows]
            for name, count in [("cold", 30), ("warm", 100)]:
                values[name] = [r["entry_ms"] for r in chat if r["label"] == label and r["condition"] == name]
                assert len(values[name]) == count, (mode, label, name, len(values[name]))
            metrics[mode][label] = {name: {"n": len(v), "p50_ms": percentile(v, .5), "p95_ms": percentile(v, .95)} for name, v in values.items()}
    evidence = {}
    for label, filename in [
        ("浏览器工具与重启", "browser-tool-after-fix.json"),
        ("浏览器待答交互重连", "browser-interaction-final.json"),
        ("浏览器取消", "browser-cancel-final.json"),
        ("浏览器页面刷新与原交互恢复", "browser-reload-after-fix.json"),
        ("冻结 EXE 工具执行与重启", "frozen-tool-lazy-fixed.json"),
        ("代码身份升级", "upgrade-code-before-fix.json"),
        ("旧协议升级", "upgrade-protocol.json"),
        ("可选能力默认关闭", "optional-default-final.json"),
        ("可选能力正常启用", "optional-enabled-verified.json"),
        ("三种可选能力故障", "optional-faults-final.json"),
        ("真实 MCP 子进程故障", "optional-mcp-failure-verified.json"),
        ("执行中最终退出与保存", "active-shutdown-final.json"),
    ]:
        value = json.loads((out / filename).read_text(encoding="utf-8"))
        assert value.get("passed", value.get("same_execution")) is True, filename
        evidence[label] = {"file": filename, "result": value}
    web = []
    for name in ["web-client-runtime-ack", "session-event-gate", "ws-event-dedup", "chat-store-streaming", "history-pagination", "history-record-reassembler", "cross-session-message", "i18n-locales"]:
        raw = (out / f"web-{name}.log").read_bytes()
        text = raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8", errors="replace")
        passed = re.search(r"(?:#|ℹ) pass (\d+)", text)
        failed = re.search(r"(?:#|ℹ) fail (\d+)", text)
        assert passed and failed and int(failed[1]) == 0, name
        web.append({"suite": name, "passed": int(passed[1]), "failed": 0})
    changed = subprocess.check_output(["git", "diff", "--name-only", "551ac7a11", args.code_commit, "--", "jiuwenswarm", "scripts/jiuwenswarm.spec"], cwd=root, encoding="utf-8").splitlines()
    assert changed == [
        "jiuwenswarm/channels/web/frontend/src/services/webClient.ts",
        "jiuwenswarm/channels/web/frontend/tests/webClientRuntimeAck.test.mjs",
        "scripts/jiuwenswarm.spec",
    ], changed
    baseline = "3fea8d1623a13646e935df9e96100ab7ad470aa4"
    frozen_paths = [
        "pyproject.toml", "uv.lock", "jiuwenswarm/channels/cli",
        "jiuwenswarm/channels/web/frontend/package.json",
        "jiuwenswarm/channels/web/frontend/package-lock.json",
    ]
    subprocess.run(["git", "diff", "--exit-code", baseline, args.code_commit, "--", *frozen_paths], cwd=root, check=True)
    raw = (out / "worker-front-desktop-regression.log").read_bytes()
    regression = raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8")
    assert "74 passed, 1 skipped" in regression
    raw = (out / "web-build.log").read_bytes()
    build = raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8")
    assert "built in" in build
    assert sum(item["passed"] for item in web) == 47
    result = {"generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "code_commit": args.code_commit, "baseline": "3fea8d1623a13646e935df9e96100ab7ad470aa4",
        "metrics": metrics, "functional_evidence": evidence, "web_tests": web,
        "scope_since_previous_audit": changed, "dependency_lock_unchanged": True,
        "preserved_paths_checked": frozen_paths,
        "limitations": ["Sandbox 实际运行仅支持 Linux，本次 Windows 验证明确平台错误与基础功能不受影响",
            "Zen 在当前 develop 被硬禁用；验证开关不会启动探测及模型列表为空",
            "可选能力使用代表性正常/并发故障组合，不代表所有配置参数组合的穷举",
            "真实进程和工具恢复使用本地模型协议 fixture；外部真实模型续聊见此前独立验收",
            "旧协议升级采用原进程所有者先关闭旧 Worker，再由新版创建 Worker；不迁移执行，不自动重放工具",
            "冻结 EXE 验证的是 Front/Worker 后端和执行恢复；最新 Web 源码单独通过真实浏览器和生产构建验证，未验证完整桌面安装器升级",
            "可选能力验收针对启动与基础执行隔离；未验证外部 PersonalContext 采集、Proactive 推荐或真实视觉模型业务"]}
    (out / "remaining-acceptance-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = []
    for mode, versions in metrics.items():
        for name, sample in versions["candidate"].items():
            requirement = 300 if name == "listen" else 2500 if name == "cold" else 500
            rows.append([mode, name, str(sample["n"]), f"{versions['baseline'][name]['p95_ms']:.2f}", f"{sample['p50_ms']:.2f}", f"{sample['p95_ms']:.2f}", str(requirement), "通过" if sample["p95_ms"] <= requirement else "未达标"])
    def table(headers, values):
        return '<div class="scroll"><table><tr>' + ''.join('<th>' + html.escape(x) + '</th>' for x in headers) + '</tr>' + ''.join('<tr>' + ''.join('<td>' + html.escape(str(x)) + '</td>' for x in row) + '</tr>' for row in values) + '</table></div>'
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Issue 4689 剩余验收结果</title><style>body{margin:0;background:#eef2f7;color:#25364c;font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1180px;margin:28px auto;background:white;padding:30px;border-radius:12px}h1{font-size:28px}h2{margin-top:30px;font-size:22px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{border:1px solid #d5dfe9;padding:10px;text-align:left;vertical-align:top}th{background:#eaf1fa}a{color:#1765bd}.scroll{overflow:auto}.note{padding:16px;background:#fff3d9;border-left:4px solid #bd8821}@media(max-width:700px){main{margin:0;padding:18px}table{min-width:650px}}</style><main>'
    page += '<h1>Issue #4689 · 阶段二三四最终复核与补验</h1><p>日期：2026-10-12。产品代码：<code>' + args.code_commit + '</code>；原 develop：<code>3fea8d162</code>。</p>'
    failures = [row for row in rows if row[-1] == "未达标"]
    page += '<p class="note">此前未做的浏览器交互、冻结 EXE、旧协议/代码升级以及可选能力代表性故障验收已实际执行。验收中修正了新页面未建立执行恢复订阅，以及冻结包 DLL 来源和惰性模块收集问题。性能使用当前代码重新采样；' + ('仍有未达标指标，见下表。' if failures else '本次采样性能指标全部达标。') + '依赖版本与锁文件保持不变，代码收束到阶段二、三、四。</p>'
    page += '<h2>重新采样的性能结果</h2><p>Windows 源码、相同解释器及锁定依赖；baseline/candidate 交错采样；每模式、每版本 30 次 Front 冷启动、30 次冷 Worker 和 100 次已有 Worker 请求。冷启动为进程未运行，未清空系统文件缓存。Front 从父进程启动计时；控制面计到成功响应；chat 计到必需执行准备后的真实 DeepAgent.send_input 入口，模型返回不计入。</p>'
    page += table(["模式", "指标", "样本", "develop P95/ms", "当前 P50/ms", "当前 P95/ms", "要求/ms", "结果"], rows)
    page += '<p>原始时间戳：' + '、'.join(f'<a href="{name}.jsonl">{name}</a>' for name in ['front-agent','front-code','chat-agent','chat-code']) + '。</p>'
    page += '<h2>真实功能验收</h2><div class="scroll"><table><tr><th>场景</th><th>结果及证据</th></tr>'
    for label, value in evidence.items():
        r = value["result"]
        brief = {k:r[k] for k in ["passed","worker_pid","same_execution","same_instance","original_chat_submissions","side_effects","old_worker_pid","new_worker_pid","mcp_child_started","chat_completed","shutdown_ms","native_process_exit_code"] if k in r}
        page += '<tr><td>' + html.escape(label) + '</td><td>通过；<a href="' + value['file'] + '">JSON 原始证据</a><br>' + html.escape(json.dumps(brief, ensure_ascii=False)) + '</td></tr>'
    page += '</table></div><p>浏览器使用隔离 Chromium 和实际 Web 页面；通过页面按钮提交、回答和取消。原 Worker、实例和 chat 执行保持一致；输入只提交一次。重启时的连接错误作为预期断线事件保留。页面刷新恢复在修正前失败、修正后通过，失败记录没有删除。</p>'
    page += '<p>最终退出单独在原执行仍 running、第二次模型请求被阻塞时验证：合法所有者停止接收、取消排空、写回历史和 metadata，再关闭 KVCache、Kernel、IPC；用关闭前持有的 Windows 原生进程句柄确认退出，避免把 PID 查询当作退出证明。关闭总预算为 30 秒，工具副作用只发生一次。</p>'
    page += '<h2>冻结构建与修正</h2><p>使用官方 scripts/jiuwenswarm.spec 构建实际 Windows EXE。首次包中的 OpenSSL DLL 来自另一套 Python，缺少 _ssl.pyd 所需的 X509_STORE_get1_objects；随后暴露 Auto-Harness 惰性模块未被静态分析收集。现在固定 DLL 为构建解释器的实际依赖，并收集改造涉及的惰性模块。<a href="frozen-ssl-probe-before-fix.json">修正前探测</a>、<a href="frozen-ssl-probe-after-fix.json">修正后探测</a>、<a href="frozen-ssl-analysis.json">符号诊断</a>。</p>'
    page += '<h2>回归与范围</h2>' + table(["Web 回归", "通过", "失败"], [[r['suite'],r['passed'],r['failed']] for r in web])
    page += '<p>本轮 Worker/Front/桌面契约及真实 Front 模块检查：<a href="worker-front-desktop-regression.log.gz">74 passed、1 skipped</a>；<a href="web-build.log.gz">Web 生产构建通过</a>。自此前 551ac7a11 复核后的产品变化仅为 Web 的活动会话恢复订阅和冻结打包配置；其他新增文件为验收驱动、测试与附件。远程 TUI、Process CLI 传输及依赖锁未修改。</p>'
    page += '<h2>六项目标的判断</h2><p>目标 1、2、6 按上表逐模式给出实测结果。目标 3 的权威业务响应、目标 4 的 Front 模块隔离及 warming 控制面、目标 5 的 Worker 常驻/同执行重连，均有本轮真实进程或浏览器证据。未达标性能依用户约束记录，不扩大到 agent-core。</p>'
    page += '<h2>与 Issue 及改进方案的符合性</h2><p><a href="https://gitcode.com/openJiuwen/jiuwenswarm/issues/4689">Issue #4689</a> 原文已重新获取并与此前审查快照核对一致。阶段二补齐 Worker 执行所有权、带游标的恢复、业务交付确认、数据隔离及关闭；阶段三接入 Kernel 租约、完整资源身份和单次 RunContext；阶段四完成轻量 Front、依赖编排、可选能力隔离、常驻复用及 Windows/冻结兼容。未发现计划外产品功能修改。</p>'
    page += '<p>阶段三沿用改进方案：复用现有 Session Coordinator/执行 handles，保留有状态 Rail/工具及真实 session claim/pin；没有照搬原文中完全替换 session-bound prewarm 的理想结构。没有增加 WarmSpare 或多 Worker 调度。实现与功能验收符合已批准方案；由于上述未达标项，不能认定 Issue 全部性能验收通过。</p>'
    page += '<p><a href="issue4689-live.json">最新 Issue 原文快照</a>；<a href="../review-20261011/issue4689-scope-and-regression-review.html">阶段二三四完整范围与此前回归证据</a>。</p>'
    page += '<p><a href="final-scope.json">当前提交逐文件范围核对</a>；<a href="evidence-archive-manifest.json">压缩原始日志清单及哈希</a>。此前回归的完整 Process CLI 套件与外部真实模型/SDK/LSP 续聊结果见关联报告；本轮单独列出新增 Web 和冻结兼容回归，不将历史全仓检查冒充最新重跑。</p>'
    page += '<h2>验收边界</h2><ul>' + ''.join('<li>' + html.escape(item) + '</li>' for item in result['limitations']) + '</ul>'
    page += '<p><a href="remaining-acceptance-results.json">机器可读完整结果</a>；<a href="../completion-check-20261012/issue4689-completion-check.html">此前复核报告</a>。</p></main><style>body{overflow-wrap:anywhere}td{overflow-wrap:anywhere}</style></html>'
    target = out / "issue4689-remaining-acceptance.html"
    target.write_text(page, encoding="utf-8")
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.paths += [v for k,v in attrs if k == "href"]
    parser = Links()
    parser.feed(page)
    local_paths = [p for p in parser.paths if not p.startswith(("http://", "https://"))]
    assert all((out / p).resolve().is_file() for p in local_paths)
    assert "�" not in page
    (out / "report-validation.json").write_text(json.dumps({"relative_links": len(local_paths), "missing": [], "sha256": hashlib.sha256(page.encode()).hexdigest()}, indent=2), encoding="utf-8")
    print(json.dumps({"code_commit": args.code_commit, "unmet": failures, "web_passed": sum(r['passed'] for r in web)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="artifacts/4689/remaining-acceptance-20261012")
    parser.add_argument("--code-commit", required=True)
    main(parser.parse_args())
