---
name: paper-gen
description: |
  CCF BDCI 2026 论文生成系统端到端编排：conception → planning → experiment → writing 四阶段串联。
  把 4 个 stage skill 当黑盒 subprocess 调，跨 stage 通过 status.json 协议聚合，
  顶层产物在 run_summary.json，writing 阶段产出 paper.pdf（ICLR 2024 LaTeX）。
  Use when 用户给研究输入后端到端跑 4 stage smoke（出 paper.pdf）。
  Do NOT use for 单 stage 调试（直接调 conception/planning/experiment/writing skill）。
version: "0.4"
kind: swarm-skill
phases:
  - title: conception
    detail: 文献检索 + 构思生成
  - title: planning
    detail: 方法设计 + 实验规划 + 门禁校验 + 写产物
  - title: experiment
    detail: 数据准备 + 实现 + 执行 + 分析
  - title: writing
    detail: 证据合同驱动的章节写作、专项审查、修订与 ICLR PDF 门禁
---

# Paper-Gen 端到端 Skill

## 目标

把 `conception → planning → experiment → writing` 4 stage 端到端跑通，产出可投递的
`paper.pdf`（ICLR 2024 LaTeX 模板）。每个 stage 当作黑盒 subprocess 调，跨 stage
通过 status.json 协议聚合状态；writing 输入侧由 `paper-gen/scripts/_writing_adapter.py`
保留逐运行账本、配置快照和资产哈希后，把 stage1+2+3 投影为 m1/m2/m3 三个 JSON。

模块三的结构化 REPLAN 会生成独立 `replan_round_n/` 反馈包，再写入独立的
`stage2_planning_replan_n/` 与 `stage3_experiment_replan_n/`；历史尝试保留供审计。

## 使用前提

1. `D:/jiuwenswarm/jiuwenswarm/resources/agent/workspace/skills/conception/scripts/main.py`
   必须存在（30 行桥接）。
2. `D:/jiuwenswarm/jiuwenswarm/resources/agent/workspace/skills/writing/scripts/main.py`
   必须存在（薄壳 CLI，接收 --module1/2/3 + --output-dir）。
3. 火山代理 `127.0.0.1:8000` 必须在跑（`scripts/llm_volcengine_proxy.py`）。
4. `.env` 凭证：`C:/Users/86187/.jiuwenswarm/config/.env`。
5. Tectonic LaTeX 工具链——正式 writing 只在 Tectonic 编译和 PDF 门禁通过后发布 PDF；
   预览或 mock 产物不是正式论文交付。
6. 模块三通过 `experiment/references/execution-capabilities.json` 发布实际可执行能力；
   planning 在首次 LLM 调用前进行确定性握手。命中未实现的任务会返回
   `unsupported_execution_capability`，paper-gen 顶层返回 `blocked_execution_capability`。

## 调用

```bash
cd D:/jiuwenswarm
uv run python -m jiuwenswarm.resources.agent.workspace.skills.paper_gen.scripts.main \
    --input-dir mock/run1 \
    --output-dir out/run1 \
    [--dry-run] [--force] [--dataset-cache-dir <path>]
```

## 产物结构

```
out/run1/
├── run_summary.json                # 顶层：4 stage 状态汇总（含 writing 终态）
├── paper_gen.log                   # paper-gen 主进程 + 4 stage 子进程日志（stderr+文件双写）
├── stage1_conception/
│   ├── conception_output.json      # 6 字段 + domain + resource_constraints
│   ├── status.json + progress.json
├── stage2_planning/
│   ├── method_design.json / experiment_plan.json
│   ├── data_plan.json / budget_report.json / execution_config.json
│   ├── 5 个 .md 摘要
│   ├── status.json + progress.json
├── stage3_experiment/
│   ├── request.json                # adapt-planning 输出
│   ├── implementation-manifest.json
│   ├── runs/<run_id>/outputs/experiment-module-output.json
│   ├── runs/<run_id>/outputs/artifact-manifest.json
│   ├── status.json
├── replan_round_n/                 # 结构化反馈 + 上一轮 planning 快照（如触发 REPLAN）
├── stage2_planning_replan_n/       # 版本化重规划（如触发 REPLAN）
├── stage3_experiment_replan_n/     # 版本化重实验（如触发 REPLAN）
├── stage4_writing/                 # Stage 4: 专职 writers + 专项 reviewers + ICLR PDF
│   ├── _input/
│   │   ├── m1.json                 # 适配自 stage1.conception_output.json
│   │   ├── m2.json                 # 适配自 stage2.{method_design,experiment_plan}
│   │   └── m3.json                 # 适配自真实 run ledger，含执行配置与资产 provenance
│   ├── paper.pdf                   # 通过 Tectonic 与 PDF 门禁的 ICLR 2024 格式 PDF
│   ├── paper.tex                   # LaTeX 源文件
│   ├── sections/                   # 六个章节的结构化正文与 handoff
│   ├── reviews/                    # claim、literature/novelty、argument、visual 专项审查
│   ├── writing.log                 # writing 子进程日志（stderr+文件双写）
│   ├── publication_eligibility.json # 正式发布资格与逐项门禁结果
│   ├── status.json + progress.json
```

## 跨 stage 协议

- **status.json**—— 4 stage 统一 schema（conception/planning/experiment/writing 各自 _status.py 写盘）
  - `status`: complete / error / preflight_failed / aborted / ...
  - `artifacts`: 产物绝对路径
  - `errors` / `warnings`
  - `wall_time_seconds` / 视 stage 而定（writing 多 revision_rounds_used / verdict / pdf_path）
- **顶层终态**——`complete` 只表示正式论文发布门禁全部通过；其他常见终态包括
  `blocked_execution_capability`、`waiting_for_experiment_replan`、
  `blocked_writing_release` 和各阶段 `aborted_*`。
- **publication_eligibility.json**——重新从落盘产物核验 writing status/verdict、四项专项审查、
  evidence graph audit、final reviewer、PDF validation 以及 `paper.tex`/`paper.pdf`。
  writing 的 `partial` 只保留诊断和修订产物，绝不作为成功退出或正式论文交付。
- **progress.json**（paper-gen 协议）—— 500ms mtime 轮询，phase 变化时打日志
  - conception: 1 帧
  - planning: 5 帧
  - experiment: 由 experiment 子进程自己写
  - writing: 以 paper contract、章节写作、整合、专项审查、修订和 PDF 门禁记录进度

## writing 阶段特殊说明

- **入参 3 JSON**：writing 子进程接收 `--module1/2/3`，不接受 `--input-dir`。
  适配层在 `paper-gen/scripts/_writing_adapter.py`，写到
  `stage4_writing/_input/{m1,m2,m3}.json`，不把计划记录冒充为已执行结果。
- **status 协议转换**：writing 内部 status 值（success/partial/failed/aborted/preflight_*/load_inputs_failed）
  通过 `_stage_runner._WRITING_STATUS_VALUE_MAP` 二次映射到 paper-gen 期望的
  `complete/partial/error/aborted/preflight_failed/load_inputs_failed`。
  所有 writing 终态均带可路由的 `verdict`；例如 `input_contract_failed`、
  `controlled_audit_failed`、`revision_no_progress` 或 `pdf_validation_failed`，不会用
  `unknown` 隐藏实际失败原因。
- **退出码**：writing 子进程只有 complete 返回 0；paper-gen 顶层也只有通过
  `publication_eligibility.json` 的 complete 返回 0。partial 和所有 blocker 均返回非 0。
- **不走 human review**：paper-gen 端到端模式调 `--no-human-review`；写作仍运行程序门禁和专项 reviewer，不增加用户人审 checkpoint。
- **视觉资产**：writing 先筛选模块三候选图。只有路径、清单、单实验范围与图义契约均可验证的
  候选图才能复用；不满足时由确定性绘图器从真实指标重绘。短图自动收紧画布，宽表按指标分面而
  非缩小字体，所有资产仍保留 source snapshot 与 hash。

## mock/run1 输入

`00_user_request.json` schema 详见 `conception/references/conception-schemas.md §1.1-1.4`。
修改后重跑即可生成新研究方向。
