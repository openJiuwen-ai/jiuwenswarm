---
name: ascend-moe-optimizer-trace-analyzer
description: 在用户提供 Chrome/Perfetto trace.json、或排查 Ascend 上 MoE/FusedDeepMoe 等算子性能时使用。按 phase、category、core group、tid 统计耗时、overlap、bubble，输出 CSV、Markdown 报告与确定性诊断；可选外部 LLM 扩写分析。默认 phase 映射面向 UMDK FusedDeepMoe，其它 trace 需替换或扩展 config/phase_map.yaml。
---

# Ascend MoE 性能 Trace 分析

分析 Chrome/Perfetto 风格的 `trace.json`，把原始 trace event 转换为结构化统计表、图表和 Markdown 报告，用于替代人工在 Perfetto 中做第一轮耗时分布和瓶颈定位。本 skill 的内置名称为 `ascend-moe-optimizer-trace-analyzer`；当前目录为 `ascend-moe-optimizer-trace-analyzer`。

## 何时使用

- 用户需要分析 **算子或 runtime 打点** 导出的 **Chrome/Perfetto `trace.json`**，关注 **phase 分布、category、Ascend core group、线程 tid、overlap、bubble**。
- 调优 **Ascend 上 MoE / FusedDeepMoe（如 `fused_deep_moe`）** 或需沿用本仓库默认 `config/phase_map.yaml` 的场景。
- 需要 **确定性自动诊断**，或可选的 **`--llm-analysis`** 二次解读。

## 脚本位置

- 用户安装后的 skill 根目录：`<ASCEND_MOE_OPTIMIZER_SKILL>` = `~/.jiuwenswarm/agent/workspace/skills/ascend-moe-optimizer-trace-analyzer`
- 入口：`<ASCEND_MOE_OPTIMIZER_SKILL>/app.py`
- 从本仓库资源运行时，将上述路径换为 `jiuwenswarm/resources/agent/workspace/skills/ascend-moe-optimizer-trace-analyzer`（相对仓库根目录）。

执行命令前请先 `cd` 到 `<ASCEND_MOE_OPTIMIZER_SKILL>`，或使用下文绝对路径形式的 `python3 .../app.py`。

## 能力概览

本 skill 面向的核心对象是 `trace.json`，不是某一个固定算子。它本身负责：
- 解析 trace 中的完整区间事件。
- 将原始 trace name 映射为可稳定统计的 phase。
- 按 phase、category、core group、tid、raw name 聚合耗时。
- 计算 phase overlap 和外层阶段 bubble。
- 生成统计图、文字化统计摘要和 Markdown 报告。
- 生成稳定、可复现的自动诊断。
- 可选调用外部 LLM，把统计上下文扩写成专家分析段落。

当前仓库默认携带的 `config/phase_map.yaml` 和部分诊断规则来自 UMDK FusedDeepMoe trace 的实践经验。因此，默认配置对 FusedDeepMoe 最友好；如果要分析其他来源的 trace，应替换或扩展 phase/category 映射配置，并逐步沉淀对应领域的诊断规则。

## Agent 执行原则
执行本 skill 时，agent 不应把文档中的示例路径当成固定输入。应先从用户请求或当前工作区中确认以下上下文，并把它们替换到命令中：

- `TRACE_JSON`：必需，用户要分析的 trace 文件。
- `OUTPUT_DIR`：必需或由 agent 选择，建议按本次任务命名，例如 `output/<case_name>`。
- `PHASE_MAP`：可选，phase/category 映射配置。若用户指定算子或已有对应配置，应使用对应配置；否则使用默认 `config/phase_map.yaml`。
- `SOURCE_ROOT`：可选，算子源码工程目录，例如某个 UMDK 工程。当前 CLI 尚未消费该参数，但 agent 可以用它阅读源码、理解打点语义和辅助维护 phase map。
- `OPERATOR`：可选，用户指定的算子名，例如 `fused_deep_moe`。当前 CLI 尚未消费该参数，但 agent 应用它选择或维护对应的 phase/category 规则和诊断上下文。

如果用户只提供 `trace.json`，按 trace-only 模式分析。如果用户同时提供源码目录和算子名，agent 应先阅读相关源码打点，再决定是否需要补充或调整 `PHASE_MAP`。

## 执行命令

在 `<ASCEND_MOE_OPTIMIZER_SKILL>` 目录下执行（以下 `<ASCEND_MOE_OPTIMIZER_SKILL>` 含义见「脚本位置」）：

基础命令模板：

```bash
cd <ASCEND_MOE_OPTIMIZER_SKILL>
python3 app.py \
  --trace <TRACE_JSON> \
  --phase-map <PHASE_MAP> \
  --output-dir <OUTPUT_DIR>
```

常用参数：
- `--trace PATH`：输入 trace JSON，必填。
- `--phase-map PATH`：phase/category 映射配置，默认 `config/phase_map.yaml`。
- `--output-dir DIR`：输出目录，默认 `output`。
- `--top-n 20`：控制 `report.md` 中各表展示的行数。
- `--llm-analysis`：启用 LLM Analysis 章节。
- `--llm-command "<cmd>"`：外部 LLM 命令，命令从 stdin 读取 prompt，并把分析文本写到 stdout。
- `--llm-timeout 120`：LLM 命令超时时间，单位秒。

如果使用默认 phase map，可以省略 `--phase-map`：

```bash
cd <ASCEND_MOE_OPTIMIZER_SKILL>
python3 app.py \
  --trace <TRACE_JSON> \
  --output-dir <OUTPUT_DIR>
```

如果本机安装了 `matplotlib`，运行时会默认生成统计分析总图 `analysis_charts.png`，并嵌入 `report.md`。未安装时会跳过图表，其他输出不受影响。

LLM 命令也可以用环境变量配置：

```bash
export TRACE_ANALYSIS_LLM_CMD="<your-llm-cli>"
cd <ASCEND_MOE_OPTIMIZER_SKILL>
python3 app.py \
  --trace <TRACE_JSON> \
  --phase-map <PHASE_MAP> \
  --output-dir <OUTPUT_DIR> \
  --llm-analysis
```

如果未启用 `--llm-analysis`，仍会生成 `llm_prompt.md`，方便后续手动交给 Codex 或其他模型复核。

## 输入要求
支持两种 trace 文件外层格式：
- `{ "traceEvents": [...] }`
- 直接以事件数组 `[...]` 作为文件内容

支持的事件类型：
- `ph == "X"`：完整区间事件，直接使用 `ts + dur` 得到结束时间。
- `ph == "B" / "E"`：按 `(pid, tid, name)` 栈式配对为完整区间。

每个可分析事件至少应包含：
- `name`：事件名称。
- `ts`：开始时间或 B/E 时间戳。
- `dur`：仅 `X` 事件需要。
- `pid` / `tid`：进程和线程维度，建议保留。
- `args`：可选，若包含 `core_type/core_id/rank_id/extra_id/event_id` 等字段，报告会一并保留。

不匹配 `--phase-map` 的事件当前不会进入 phase 统计表。分析非默认 trace 时，最重要的适配工作就是维护一份能覆盖目标 trace name 的 phase mapping。

分析时会同时保留：
- `name`：原始 trace name。
- `normalized_name`：去掉 `[extra:x] #seq` 后的归一化名称，便于把同一类事件合并统计。

## Phase 和 Category

本 skill 通过 `--phase-map` 指定的 YAML 配置把原始 trace name 映射到稳定 phase。配置包含两类信息：
- `phases`：phase 到正则 pattern 列表的映射。
- `phase_categories`：phase 到 category 的归因。

正则命中多个 phase 时，优先选择 pattern 字符串最长的更具体规则。

默认 category 包括：
- `container`
- `wait`
- `sync`
- `compute`
- `epilogue`
- `communication`
- `quant`
- `init`
- `cleanup`
- `other`

对于 UMDK FusedDeepMoe，默认配置已经覆盖 `processing`、`dispatch_gmm1`、`gmm2_combine` 及其子阶段。对于其他 trace，可以保留这套统计框架，只替换 phase/category 映射。

## Core Group

本 skill 会尽量为每个已映射事件补充：
- `core_type`
- `core_group`
- `core_kind`
- `core_id`

当前内置的核组解释来自 UMDK 1C2V trace：
- `type0 -> cube`
- `type1 -> vector_recv`
- `type2 -> vector_send`

如果 trace event args 中没有 `core_type/core_id`，本 skill 会尝试从 `tid` 推断，例如 `type1_core003 -> vector_recv/core_id=3`。

对于其他来源的 trace，如果没有这类 `core_type` 约定，事件会落到 `unknown` 核组。后续若要支持更多硬件或 runtime，可以把 core group 规则从当前内置逻辑中抽成配置。

## 指标口径
- `total_us`：同类事件时长直接求和，会重复累计并行 tid/core。
- `union_us`：同类事件时间区间并集长度，更接近 wall time 覆盖。
- `ratio_to_total_wall = union_us / trace_wall_time`。
- `ratio_to_core_group_wall = union_us / 当前 core_group 的 union_us`，用于判断某类耗时在该核组内部的覆盖比例。
- `ratio_to_core_group_wall` 是覆盖率，不是互斥占比；不同 category/phase 可以在同一时间重叠，因此同一核组下的百分比不要求加和为 100%。
- `overlap_summary.csv` 的 overlap 基于 phase 区间并集两两求交，避免逐事件重复累计。
- `bubble_summary.csv` 表示外层阶段中未被已知子阶段覆盖的时间空洞。这是“未归因时间”，不一定代表硬件空闲。

## 输出文件
- `phase_instances.csv`：每个已映射区间事件，包含 phase/category/name/core_group/core_id/timing。
- `phase_summary.csv`：按 phase 聚合。
- `category_summary.csv`：按 category 聚合。
- `core_group_summary.csv`：按 core group 聚合。
- `phase_core_group_summary.csv`：按 `(core_group, phase)` 聚合。
- `category_core_group_summary.csv`：按 `(core_group, category)` 聚合。
- `name_summary.csv`：按原始 trace name 聚合。
- `phase_tid_summary.csv`：按 `(phase, pid, tid)` 聚合，用于看单线程或单核长尾。
- `overlap_summary.csv`：phase 两两 overlap。
- `bubble_summary.csv`：外层阶段内部 bubble。
- `summary.json`：整体概览。
- `diagnosis.json`：确定性自动诊断结果。
- `statistical_summary.md`：确定性统计摘要，文字化说明图表和关键统计信号。
- `llm_prompt.md`：交给 LLM 的完整统计上下文，总是生成。
- `llm_analysis_meta.json`：LLM 调用状态、命令和错误信息，总是生成。
- `llm_analysis.md`：启用 LLM 且命令成功时生成。
- `report.md`：可读报告，包含 Overview、Visualizations、Statistical Highlights、Automatic Diagnosis、可选 LLM Analysis 和各类汇总表。
- `analysis_charts.png`：安装 `matplotlib` 时默认生成。单图包含 core group wall 覆盖、非 container category 的 `total_us` 饼图和 top phase。完整 trace 时间线建议继续使用 Perfetto UI 查看。

## 诊断策略
报告优先回答：
1. 哪些 phase 覆盖 wall time 最多。
2. 耗时类型更偏 wait、sync、compute、epilogue、communication 还是 quant。
3. 耗时主要落在哪些 core group 或 tid。
4. 关键 phase 之间的 overlap 是否不足。
5. 外层阶段内部是否存在明显未归因 bubble。
6. top raw names 中哪些原始事件应优先回查。

当前确定性诊断仍包含一部分 UMDK FusedDeepMoe 经验规则，例如 `dispatch_gmm1` 与 `gmm2_combine` 的 overlap 判断。分析其他 trace 时，这些规则可能只具备参考价值；通用统计表和图表仍然是主要输出。

## 依赖和验证
默认运行只使用 Python 标准库，不需要安装第三方包。

可选能力：
- `matplotlib`：用于自动生成 `analysis_charts.png`。
- 外部 LLM CLI：用于 `--llm-analysis`，协议是 stdin 输入 prompt、stdout 输出分析文本。

基础验证：

```bash
cd <ASCEND_MOE_OPTIMIZER_SKILL>
python3 app.py --trace <TRACE_JSON> --phase-map <PHASE_MAP> --output-dir <OUTPUT_DIR> --top-n 20
```

## 当前限制
- 默认只分析单个 trace 文件，不做多 trace 对比。
- 当前没有显式 `--profile` 机制；不同 trace 来源主要通过 `--phase-map` 适配。
- 未映射到 phase 的事件会被过滤，通用 fallback 统计仍有改进空间。
- core group 规则目前仍以内置 UMDK 1C2V 约定为主，尚未完全配置化。
- 部分自动诊断规则仍偏 FusedDeepMoe，需要继续拆分为通用规则和领域规则。
- LLM Analysis 是可选外部命令，不内置具体模型、API key 或网络调用。
