# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Render the recorded Issue 4689 acceptance evidence without inventing samples."""
from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path


def percentile(values, fraction):
    values = sorted(values)
    return values[math.ceil(len(values) * fraction) - 1]


def summary(values):
    return {"n": len(values), "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95)}


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def generate(directory: Path):
    metrics = {}
    table = []
    for mode, suffix in (("agent", "agent"), ("code.normal", "code")):
        front = read_rows(directory / f"front-final-{suffix}.jsonl")
        chat = read_rows(directory / f"chat-final-{suffix}.jsonl")
        metrics[mode] = {}
        for label in ("baseline", "candidate"):
            rows = [row for row in front if row["label"] == label]
            assert len(rows) == 30, (mode, label, len(rows))
            group = {"listen": summary([row["listen_ms"] for row in rows])}
            for method in ("session.list", "project.list", "config.get", "history.get"):
                group[method] = summary([row["controls"][method]["available_from_spawn_ms"] for row in rows])
                group[method + ".response"] = summary([row["controls"][method]["response_ms"] for row in rows])
            for condition, count in (("cold", 30), ("warm", 100)):
                values = [row["entry_ms"] for row in chat if row["label"] == label and row["condition"] == condition]
                assert len(values) == count, (mode, label, condition, len(values))
                group[condition] = summary(values)
            metrics[mode][label] = group
        for key, name, limit in (("listen", "Front 监听", 300), ("session.list", "Session 可用", 500),
                                 ("project.list", "Project 可用", 500), ("config.get", "Config 可用", 500),
                                 ("history.get", "History 可用", 500), ("warm", "已有 Worker 进入执行", 500),
                                 ("cold", "冷 Worker 进入执行", 2500)):
            before, after = metrics[mode]["baseline"][key], metrics[mode]["candidate"][key]
            passed = after["p95_ms"] <= limit
            table.append(f'<tr><td>{mode}</td><td>{name}</td><td>{after["n"]}</td>'
                         f'<td>{before["p50_ms"]:.1f} / {before["p95_ms"]:.1f}</td>'
                         f'<td>{after["p50_ms"]:.1f} / {after["p95_ms"]:.1f}</td>'
                         f'<td>≤{limit}</td><td class="{"pass" if passed else "fail"}">{"达标" if passed else "未达标"}</td></tr>')
    (directory / "acceptance-results.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    native = []
    for name, description in (
        ("native-gateway-tool-final", "Front/Gateway 重启后原执行与工具副作用"),
        ("native-gateway-interaction-final2", "待答交互恢复并回答原问题"),
        ("native-owner-final2", "断连后完成、持久历史和应用最终退出"),
        ("native-cancel-final", "接回后取消原执行"),
        ("native-queue", "排队输入跨 Front 重启"),
        ("native-steer", "补充输入跨 Front 重启"),
        ("native-crash-final2", "Worker 崩溃后明确中断且不重放"),
        ("native-agentos-directory-a", "AgentOS 独立数据目录 A"),
        ("native-agentos-directory-b", "AgentOS 独立数据目录 B"),
        ("native-cross-session-final2", "跨会话消息与目标执行恢复"),
        ("native-mcp-final2", "真实 MCP 进程复用及工具副作用不重复"),
        ("native-warming-cancel-final", "Worker warming 期间取消排队输入"),
        ("native-failed-create-final5", "Session 创建响应交付失败触发真实 abort"),
        ("native-metadata-concurrency", "独立进程并发修改 metadata，保留执行及控制字段"),
    ):
        path = directory / (name + ".json")
        if not path.exists():
            native.append(f'<tr><td>{description}</td><td class="pending">待补齐证据</td><td>未计为通过</td></tr>')
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        details = {key: value[key] for key in ("worker_pid", "same_worker", "same_instance", "same_execution",
                   "same_target_execution", "main_model_calls", "side_effects", "offline_history_complete",
                   "target_history_complete", "supplemental_persisted", "mcp_pid_before", "mcp_pid_after",
                   "warming_cancel_ms", "create_lifecycle", "writers", "overlap_ms", "final_fields",
                   "all_fields_preserved", "shutdown", "shutdown_ms") if key in value}
        native.append(f'<tr><td>{description}</td><td><a href="{path.name}">原始记录</a></td>'
                      f'<td><code>{html.escape(json.dumps(details, ensure_ascii=False))}</code></td></tr>')
    evidence = (directory / "acceptance-notes.html").read_text(encoding="utf-8")
    page = '''<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Issue 4689 实施与验收报告</title><style>
body{margin:0;background:#f5f6f8;color:#182438;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1120px;margin:40px auto;padding:0 24px}
header{background:#142a42;color:#fff;padding:30px;border-radius:14px}h1{font-size:27px;margin:0 0 12px}h2{font-size:21px;margin-top:0}
section{background:#fff;border:1px solid #dfe5ed;border-radius:12px;padding:26px;margin:22px 0}p{margin:12px 0}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:12px 10px;border-bottom:1px solid #e2e8ef;text-align:left;vertical-align:top}th{background:#eef3f8;white-space:nowrap}code{font-size:12px;overflow-wrap:anywhere}
a{color:#1764ac}.pass{color:#117444;font-weight:700}.fail{color:#b02822;font-weight:700}.pending{color:#a1620b}.scroll{overflow:auto}.note{border-left:4px solid #ce7620;padding:8px 16px;background:#fff6e8}
details{margin:16px 0}summary{cursor:pointer;font-weight:600}.small{color:#526276;font-size:13px}@media(max-width:600px){main{margin:18px auto;padding:0 12px}header,section{padding:20px}}
@media print{body{background:white}main{max-width:none;margin:0}section,header{break-inside:avoid}a{color:inherit}}</style>
<main><header><h1>Issue #4689 · 阶段二、三、四</h1><div>Windows 源码实施与验收记录 · 报告日期 2026-10-11</div></header>
<section><h2>验收结论</h2><p class="note">冷 Worker 的 P95 尚未达到 2.5 秒，不能宣称 Issue #4689 六项目标已全部完成。本报告保留失败指标、原始样本及尚未完成的验证。</p>
<p>阶段二、三、四的仓内改造已提交。Front 监听、四类控制面与已有 Worker 的性能指标达到要求；冷 Worker 进入执行约 8 秒，保留为未达标。按用户确定的范围，保持现有依赖，不扩展到 agent-core 或其他功能修复。</p>
<p>性能使用真实 Front、原生 Worker 与 OpenJiuwen Runtime。模型响应使用本机 OpenAI 协议服务，执行入口取 DeepAgent.send_input；没有使用 ACK、队列接收或 Gateway 接收作为结束点。</p></section>
<section><h2>性能实测</h2><p class="small">单位：毫秒；P50 / P95 采用 nearest-rank。每个主要模式、每个版本均为 30 次冷启动和 100 次已有 Worker 请求。</p><div class="scroll"><table><thead><tr><th>模式</th><th>指标</th><th>样本</th><th>develop P50 / P95</th><th>改进版 P50 / P95</th><th>P95 要求</th><th>结果</th></tr></thead><tbody>TABLE</tbody></table></div>
<p>控制面可用从父进程启动 Front 到成功响应计时；另在原始记录中保留单次 RPC 耗时。chat 从请求发送到必需资源准备完毕后的 SDK 执行入口计时，模型网络返回时间不计入该指标。</p></section>
<section><h2>真实进程与工具证据</h2><p>本机协议模型使重启、暂停和工具调用可重复；实际 Runtime、历史、交互及 PowerShell 工具均运行于真实子进程。外部真实模型与 SDK 续聊单独列出。</p><div class="scroll"><table><thead><tr><th>场景</th><th>证据</th><th>结果摘要</th></tr></thead><tbody>NATIVE</tbody></table></div></section>
EVIDENCE
<section><h2>原始数据</h2><p><a href="acceptance-results.json">统计结果 JSON</a> · <a href="front-final-agent.jsonl">agent Front 样本</a> · <a href="front-final-code.jsonl">code Front 样本</a> · <a href="chat-final-agent.jsonl">agent chat 样本</a> · <a href="chat-final-code.jsonl">code chat 样本</a> · <a href="acceptance-manifest.json">环境与版本清单</a> · <a href="regression-baseline-difference.json">回归失败对照</a></p>
<p class="small">HTML 本身可离线阅读；下载完整证据包后，同目录链接可打开原始记录。</p></section></main></html>'''
    (directory / "issue4689-acceptance.html").write_text(page.replace("TABLE", "".join(table)).replace("NATIVE", "".join(native)).replace("EVIDENCE", evidence), encoding="utf-8")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    print(json.dumps(generate(parser.parse_args().directory), ensure_ascii=False))
