# AgentMoss 检测模块说明

## 1. 模块定位

`agent_moss` 是 AgentSSAS 内置的 Agent 行为安全分析模块。它将
AgentSSAS 采集的 Agent 生命周期事件转换为 AgentMoss 运行时事件，执行：

- 单事件安全规则分析；
- 同一 session 内的行为链关联分析；
- 基于程序依赖图（Program Dependence Graph，PDG）的数据泄露分析。

该模块当前运行在 AgentSSAS 的 `notify` 分析路径中：检测结果会进入模块结果库
和威胁日志，但不会直接阻断 JiuwenSwarm 的 Agent 执行。报告中的 `block`、
`ask` 是 AgentMoss 给出的建议决策，不代表 AgentSSAS 已经执行了阻断或询问。

## 2. 目录结构

```text
agent_moss/
├── README.md                  # 本说明文档
├── module.yaml               # 模块订阅、分析方法和策略配置
├── agent_moss_modeler.py     # AgentSSAS 建模插件入口
├── event_adapter.py          # AgentSSAS → AgentMoss 事件结构适配
├── agent_moss_analyzer.py    # AgentMoss 分析、历史维护和报告转换
└── engine/
    ├── events.py             # AgentMoss 事件与策略决策数据结构
    ├── policy.py             # 单事件规则和行为链策略引擎
    ├── pdg.py                # PDG 构建、标注、传播和违规检查
    └── SOURCE.md             # 内置引擎的来源版本和文件哈希
```

## 3. 运行链路

```text
JiuwenSwarm 生命周期事件
        │
        ▼
AgentSSASSecurityRail / AgentSSAS 预处理
        │  UnifiedEvent.to_event_desc()
        ▼
AgentMossModeler
        │  event_adapter.adapt_event_desc()
        ▼
AgentMoss 事件 + 显式关联 ID
        │
        ▼
AgentMossAnalyzer
        ├── 单事件规则
        ├── 同 session 行为链
        └── PDG 数据泄露分析
        │
        ▼
AgentSSAS 威胁报告 / result.db / OCSF 威胁日志
```

`AgentMossModeler` 和 `AgentMossAnalyzer` 的模型类型均为
`agent_behavior_model`，由 AgentSSAS 模块管理器完成配对和调用。

## 4. 事件结构适配

### 4.1 事件类型映射

| AgentSSAS 事件 | AgentMoss 事件 | AgentMoss payload |
|---|---|---|
| `invoke_start` | `chat_request` | `content` |
| `llm_input` | `model_call` | `messages` |
| `llm_output` | `model_output` | `response` |
| `tool_input` | `tool_call` | `tool_args`、`tool_call_id` |
| `tool_output` | `tool_result` | `tool_result`、`tool_call_id` |
| `invoke_end` | `model_output` | `response` |

不在映射表中的事件会被标记为 `unsupported_event`，不会送入 AgentMoss
策略引擎，也不会被静默解释为某种已知事件。

### 4.2 关联字段

适配结果保留下列显式关联字段：

- `trace_id`
- `session_id`
- `interaction_seq`
- `llm_call_seq`
- `tool_call_seq`
- `tool_call_id`
- `node_id`
- `event_id`
- `timestamp`

工具事件优先使用上报端的 `tool_call_id`。字段缺失时，适配器依次使用
`node_id` 或 `session_id + interaction_seq + tool_call_seq` 生成关联键。
这保证 `tool_input` 与 `tool_output` 能以显式标识关联，而不是依赖工具名称或
自然语言内容相似度进行猜测。

### 4.3 建模结果

建模结果同时包含：

- AgentSSAS 兼容字段：`event_type`、`action_name`、`input_content`、
  `output_content`、`aux_ids`、`trace`；
- AgentMoss 输入：`agentmoss_event`；
- 关联信息：`correlation`；
- 适配信息：`adapter_version`、`supported`。

示例：

```json
{
  "model_type": "agent_behavior_model",
  "adapter_version": "1.0",
  "supported": true,
  "event_type": "tool_input",
  "agentmoss_event": {
    "event_type": "tool_call",
    "subject": "bash",
    "payload": {
      "tool_args": {"command": "ls"},
      "tool_call_id": "call-001"
    },
    "session_id": "session-001",
    "request_id": "trace-001",
    "event_id": "event-001",
    "timestamp": 1787270400.0
  },
  "correlation": {
    "trace_id": "trace-001",
    "session_id": "session-001",
    "interaction_seq": 0,
    "llm_call_seq": 0,
    "tool_call_seq": 0,
    "tool_call_id": "call-001",
    "node_id": "session-001_0_toolcall_0"
  }
}
```

## 5. 分析能力

### 5.1 单事件规则

`PolicyEngine` 对当前事件执行确定性规则检查，覆盖：

- 危险 Shell 操作；
- 敏感路径访问；
- 数据外传命令；
- 特权账号操作；
- 持久化操作；
- 混淆或动态执行；
- 提示注入和安全绕过信号；
- 敏感数据模式。

### 5.2 行为链分析

分析器按 `session_id` 隔离历史事件，默认每个 session 最多保存 200 条。
当前行为链规则可识别例如：

- 敏感数据事件之后发生外部传输；
- 提示注入上下文之后发生外部传输。

缺少 `session_id` 时使用 `trace_id`；两者均缺失时，事件不会被合并到全局历史，
避免不同 Agent 或会话之间发生错误关联。

### 5.3 PDG 数据泄露分析

PDG 分析采用构建、标注、传播、检查四个阶段：

1. 将用户输入、模型输出、工具名、工具参数、工具动作、工具结果和资源构造成图节点；
2. 根据敏感数据模式、资源类型和事件来源生成初始机密性/完整性标签；
3. 沿显式数据依赖边传播安全标签；
4. 在公共外传动作发生前检查高机密数据是否可证明地到达外传节点。

当前覆盖的主要 PDG 违规包括：

- 高机密数据直接进入公共外传；
- 敏感资源进入公共外传；
- 低完整性来源控制机密数据外传；
- 敏感数据经临时资源暂存后再外传。

## 6. 证据和安全边界

AgentMoss 只将以下信息作为可证明的数据依赖证据：

- 匹配的敏感值短指纹；
- 相同资源标识；
- 相同 `tool_call_id`。

明确记录的事件顺序和前驱事件 ID 用于构造控制流、控制依赖和证据链顺序，
但事件先后本身不会被提升为数据依赖。

以下情况不会被声明为已证明的数据依赖：

- 只有自然语言语义相似；
- 数据经过无法识别的编码、加密或转换；
- 必需的生命周期 hook、payload 或关联 ID 缺失；
- 上游事件被截断、未上报或不在历史窗口内。

这类情况应视为 `unknown`，而不是“已证明安全”。当前报告可能没有 PDG
违规，但这只表示在现有事件和证据边界内未证明违规，不代表开放环境中的绝对安全。

PDG 报告只保留规则 ID、图节点 ID、资源标识和不可逆短指纹，不写入匹配到的
敏感明文。原始事件是否保存明文由 AgentSSAS 上游采集与存储策略决定，不由本模块
的 PDG 脱敏替代。

## 7. 风险报告

分析结果转换为 AgentSSAS 威胁报告，主要字段如下：

| 字段 | 说明 |
|---|---|
| `has_risk` | 风险分数是否达到 `risk_threshold` |
| `risk_level` | `safe`、`low`、`medium`、`high`、`critical` |
| `risk_type` | 当前最高优先级风险类型 |
| `risk_score` | AgentMoss 风险分数，范围 0–100 |
| `detected_threats` | 去重后的风险类型列表 |
| `recommended_actions` | 建议动作，不等价于已执行动作 |
| `evidence.findings` | AgentMoss 规则发现 |
| `evidence.event_mapping` | SSAS 与 AgentMoss 事件映射 |
| `evidence.correlation` | 显式关联字段 |
| `evidence.pdg` | 脱敏的 PDG 违规和图结构（命中时） |

风险分数到等级的转换为：

| 分数 | 等级 |
|---|---|
| `0` | `safe` |
| `1–44` | `low` |
| `45–69` | `medium` |
| `70–89` | `high` |
| `90–100` | `critical` |

## 8. 配置

模块配置位于 `module.yaml`。

### 8.1 模块级配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `enabled` | `true` | 是否加载 AgentMoss 模块 |
| `analysis_methods` | `[rule, behavior_chain, pdg]` | 当前分析能力声明；实际开关由 `policy` 配置控制 |
| `risk_threshold` | `low` | `has_risk` 的最低上报门槛 |
| `max_history_events` | `200` | 每个 session 的最大历史事件数 |
| `include_pdg_graph` | `true` | 命中 PDG 违规时是否输出脱敏图 |

### 8.2 策略配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `default_decision` | `allow` | 未达到决策阈值时的默认建议 |
| `enforcement_scope` | `behavior_chain` | 参与建议决策的风险范围 |
| `ask_threshold` | `45` | 建议询问阈值 |
| `block_threshold` | `80` | 建议阻断阈值 |
| `analyze_behavior_chain` | `true` | 是否分析行为链 |
| `analyze_pdg_data_leakage` | `true` | 是否构建并检查 PDG |
| `enforce_pdg_data_leakage` | `true` | PDG 风险是否参与建议决策 |
| `pdg_max_events` | `80` | 单次 PDG 使用的最大事件数 |
| `pdg_trusted_egress_patterns` | `[]` | 可信外传目标正则列表 |
| `pdg_egress_tool_patterns` | `[]` | 自定义外传工具名正则列表 |

`enforcement_scope: behavior_chain` 表示单事件规则仍会产生风险分数和发现，
但默认只让行为链/PDG 风险影响 AgentMoss 的 `ask`、`block` 建议。
若改为 `all`，单事件规则也会参与建议决策。无论哪种配置，当前 AgentSSAS
订阅仍是 `notify`，不会仅因这里出现 `block` 就自动阻断执行。

## 9. 结果存储

AgentSSAS 默认将该模块的分析结果写入：

```text
<ssas_home>/ssas/modules/agent_moss/result.db
```

同时，AgentSSAS 呈现层会根据全局配置生成 OCSF 威胁日志。分析过程中的
session 历史保存在当前分析器实例内，属于有界内存状态，不等同于
`result.db` 中的长期审计记录；进程重启后不会从结果库自动恢复分析历史。

## 10. 引擎来源和更新

`engine/` 是 AgentMoss 确定性分析核心的内置快照。来源基线和导入前哈希记录在
`engine/SOURCE.md`。AgentSSAS 专用的事件适配和报告转换位于 `engine/` 外，
以便后续更新 AgentMoss 引擎时进行机械比对。

更新引擎时应至少执行：

1. 比对 `events.py`、`policy.py`、`pdg.py` 与上游版本；
2. 只调整包内相对导入，不混入 SSAS 适配逻辑；
3. 更新 `engine/SOURCE.md` 的来源提交和哈希；
4. 重新验证事件映射、规则、行为链、PDG、会话隔离和敏感证据脱敏；
5. 运行 AgentMoss 原测试以及 AgentSSAS 全量测试。

## 11. 验证

在 AgentSSAS 仓库目录执行：

```bash
PYTHONPATH=src <python> -m pytest \
  tests/agent_ssas/core/detection_modules/test_agent_moss.py -q

PYTHONPATH=src <python> -m pytest \
  tests/agent_ssas/core/integration/test_pipeline_integration.py -q

PYTHONPATH=src <python> -m pytest tests -q
```

重点验证场景包括：

- 六类生命周期事件映射；
- `tool_call_id` 的保留和缺失回退；
- 危险 Shell 操作报告；
- 读取敏感数据后显式外传的 PDG 违规；
- PDG 证据中不包含敏感明文；
- 不同 session 之间不共享行为历史；
- 真实 AgentSSAS 流水线中的 notify 分析和结果落库。
