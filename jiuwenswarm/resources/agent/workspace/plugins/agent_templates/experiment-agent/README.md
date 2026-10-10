# Experiment Agent

模块三的 JiuwenSwarm Agent 模板。根 Agent 只做门禁、编排和最终交接；四个直属子 Agent 分别负责数据、实现解析、实验执行和分析可视化。

实际契约与计算代码只保存在共享 `workspace/skills/experiment`，本模板通过工具调用该确定性后端，避免 Agent 自由修改实验数据。

运行顺序：

`root adapt/initialize → data → implementation builder → root code review → environment deploy/verify → deterministic smoke → root execution review → parallel execution + live validation → analysis + 12+ evidence figures → Module 4`

状态保存在每次运行目录的 `outputs/agent-state.json`，因此同一个 `run_id` 可以查询和安全续跑；输入或实现清单摘要不一致时会拒绝复用。

Implementation Builder支持`inspect → reuse → build → test → register → request_approval`，安全执行时先登记和请求代码审查，审查通过后才由后端测试。`CODE_REVIEW_REQUIRED`和`EXECUTION_REVIEW_REQUIRED`都是可恢复状态；根Agent使用隔离审查上下文，确定性后端运行冒烟并写最终批准。审批与代码、命令、依赖、清单和冒烟SHA-256绑定，内容变化后自动失效。

Execution Agent内部可以并行多个冻结子实验，但四个JiuwenSwarm阶段子Agent仍必须串行委派。实时状态写入`outputs/execution-monitor.json`，事件写入`outputs/execution-events.jsonl`；Analysis Agent只基于真实产物生成互补候选图与`outputs/writing-evidence.md`。
