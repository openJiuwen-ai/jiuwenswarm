# Paper Generation Agent

CCF BDCI 2026 论文生成系统的长会话总控 Agent。它按
`conception → planning → experiment → writing` 严格串行调度，并维护跨阶段状态和
结构化 REPLAN 路由。

## 调用链

```text
paper-gen-agent
├── invoke_conception_subagent  → 原生 research_agent + conception Skill
├── invoke_planning_supervisor  → planning 文件/状态协议
├── invoke_experiment_subagent  → experiment-agent 及其四个专用子 Agent
├── invoke_writing_subagent     → writing 证据门禁、写作、制图与 PDF 发布
└── read_token_usage            → 分模块及全流程 Token 账本
```

总 Agent 负责上下文、阶段顺序、状态查询和反馈路由；科研计算、文件校验和状态推进仍由
各 Skill 的确定性代码完成。Persona 不重新实现检索、实验或指标计算。

## 构思阶段输出

构思工具读取用户请求后调用实际 conception CLI，并把通过校验的结果拆成规划模块可直接
读取的 8 个文件：

```text
research_question.json
hypotheses.json
gap_report.json
key_papers.json
references.json
research_frontier.json
resource_constraints.json
domain.json
```

其中 `references.json` 包含完整 10 篇核验文献；`key_papers.json` 是其关键论文子集。

## 运行状态

```text
run_dir/
├── pipeline_state.json
├── token_usage.json
├── token_usage_report.md
├── stage1_conception/
├── stage2_planning/
├── stage3_experiment/
├── stage4_writing/
├── feedback/
└── logs/
```

每个阶段必须返回结构化状态。下游发现可修复的上游缺口时返回
`replan_to:<stage>`；根 Agent 跳转到对应阶段，但不能绕过实验审批、真实指标或写作证据
门禁。超过配置的重规划上限后应终止并保留 blocker。

## 当前实现状态

- 构思：调用最新 conception Skill 和 Jiuwen 原生 `research_agent`；
- 规划：调用 planning 的真实 CLI 和状态文件，不使用伪 JSON 标准输出；
- 实验：调用真实 experiment-agent；
- 写作：调用真实 writing 阶段，仅在实验输出满足契约时生成 PDF；
- 写作终态：每一次终止均返回稳定 `verdict`，供根 Agent 区分输入、审计、修订、编译和 PDF
  校验失败，不把它们显示为 `unknown`；
- 写作图表：优先复用带单实验语义契约的模块三图，否则确定性重绘；宽表不通过缩小字体来适配页面；
- 状态：支持读取和原子更新 `pipeline_state.json`。
- Token：按阶段和尝试记录 prompt/completion/total；分别汇总最终成功链路与
  REPLAN/失败/被替代尝试。供应商未返回 usage 时明确标记缺失，不估算。

模型连接、API Key 和代理地址由 JiuwenSwarm 运行环境提供，不写入 Agent 模板。

详细设计见 [docs/design-2026-09-03.md](docs/design-2026-09-03.md)。
