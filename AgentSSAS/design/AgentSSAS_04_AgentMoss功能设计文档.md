# AgentSSAS — AgentMoss 检测模块功能设计文档

## 1. 文档目的

本文档说明 AgentMoss 作为 AgentSSAS 检测模块时的实际运行语义，包括：

- AgentSSAS 生命周期事件如何适配为 AgentMoss 运行时事件；
- 单事件规则、行为链和 Agent行为图（Agent Behavior Graph，ABG）如何共同产生风险结论；
- 会话历史、事件关联、幂等缓存与并发保护的具体实现；
- `has_risk`、`risk_level`、`decision` 和 `recommended_actions` 之间的区别；
- “仅上报风险事件”的精确边界；
- 存储、呈现、配置、测试和已知限制。

文档以当前代码为准，不把尚未实现的能力描述为已有保障。

## 2. 模块定位与边界

`agent_moss` 是 AgentSSAS 内置的 Agent 行为安全分析模块。它不负责采集
JiuwenSwarm 运行时事件，而是消费 AgentSSAS 已经预处理的统一事件。

当前模块具有三类分析能力：

1. **单事件规则检测**：识别危险 Shell、敏感路径、数据外传、持久化等信号。
2. **行为链检测**：在同一会话中关联前序敏感事件与后续外传动作。
3. **Agent行为图（ABG）数据泄漏检测**：依据显式数据身份、资源身份和工具调用关联构造运行时行为图。

当前订阅模式是 `notify`，其语义为：

- AgentSSAS 在后台异步运行 AgentMoss；
- `report_event` 不等待 AgentMoss 分析完成；
- AgentMoss 报告中的 `block` 或 `ask` 只是建议；
- 本模块不直接阻断或暂停 JiuwenSwarm 工具调用。

因此，“检出高风险”不等于“已经阻断高风险动作”。

## 3. 代码组成

```text
agent_moss/
├── module.yaml
├── agent_moss_modeler.py
├── event_adapter.py
├── agent_moss_analyzer.py
├── docs/
│   └── agent_moss_detailed_design.md
└── engine/
    ├── events.py
    ├── policy.py
    ├── agent_behavior_graph.py
    └── SOURCE.md
```

| 文件 | 职责 |
|---|---|
| `module.yaml` | 声明模块、事件订阅、输出策略和分析参数 |
| `agent_moss_modeler.py` | AgentSSAS 数据建模插件入口 |
| `event_adapter.py` | AgentSSAS 事件到 AgentMoss 事件的确定性结构适配 |
| `agent_moss_analyzer.py` | 历史管理、分析调度、风险等级和 AgentSSAS 报告转换 |
| `engine/events.py` | `PolicyDecision`、`EventRecord` 等运行时数据结构 |
| `engine/policy.py` | 确定性单事件规则、行为链规则和建议决策 |
| `engine/agent_behavior_graph.py` | ABG 构建、标签传播与公共外传违规检查 |
| `engine/SOURCE.md` | 内置引擎快照的来源基线和导入前哈希 |

`engine/` 是从本地 JiuwenSwarm 中的 `agentmoss/agentmoss_core` 导入的确定性
分析内核快照，来源基线提交为 `bd73bca9`。AgentSSAS 专用的事件适配和
报告转换保持在 `engine/` 之外，便于后续机械比对上游引擎。

## 4. 模块加载与运行链路

AgentSSAS 启动时，`DetectionModuleManager` 扫描模块目录并解析
`module.yaml`。建模插件和分析插件通过共同模型类型进行配对：

```text
AgentMossModeler.model_type
        = "agent_behavior_model"
        = AgentMossAnalyzer.expected_model_type
```

单个事件的完整路径为：

```text
JiuwenSwarm lifecycle hook
  → AgentSSASSecurityRail
  → raw_event
  → AgentSSAS 预处理
  → UnifiedEvent.to_event_desc()
  → AgentMossModeler.build_model()
  → adapt_event_desc()
  → AgentMossAnalyzer.analyze()
      ├── 单事件规则
      ├── 行为链
      └── ABG
  → AgentSSAS 简化威胁报告
      ├── has_risk=true
      │   ├── modules/agent_moss/result.db
      │   ├── reports/threat_log/*.json
      │   └── ssas_core.db/alerts
      └── has_risk=false
          └── 不生成 AgentMoss 结果记录和告警
```

无风险报告的输出过滤发生在 `AgentMossAnalyzer.analyze()` 完成之后。
因此，输出过滤不会跳过 AgentMoss 分析，也不会丢失用于后续行为链和
ABG 的安全事件历史。

## 5. AgentSSAS 输入契约

### 5.1 输入主体

`AgentMossModeler` 接收 `UnifiedEvent.to_event_desc()` 产生的字典。模块依赖的
主要字段如下：

| 路径 | 用途 |
|---|---|
| `event_id` | 事件幂等缓存与证据节点标识 |
| `event_node.event_type` | 选择 AgentMoss 事件类型 |
| `event_node.action_name` | 工具或动作主体，如 `bash`、`read_file`、`send_http` |
| `event_node.input_content` | 用户输入、模型输入或工具参数 |
| `event_node.output_content` | 模型输出或工具结果 |
| `event_node.timestamp` | 会话内事件排序 |
| `event_node.node_id` | 节点溯源及 `tool_call_id` 缺失时的回退值 |
| `aux_ids.trace_id` | AgentSSAS 链路追踪，同时映射为 AgentMoss `request_id` |
| `aux_ids.session_id` | 历史隔离的首选键 |
| `aux_ids.interaction_seq` | 交互序号 |
| `aux_ids.llm_call_seq` | LLM 调用序号 |
| `aux_ids.tool_call_seq` | 工具调用序号 |
| `aux_ids.tool_call_id` | `tool_input` 与 `tool_output` 的显式配对标识 |
| `aux_ids.agent_id` | AgentMoss `agent_name` |
| `trace.trace_id` | `aux_ids.trace_id` 缺失时的追踪 ID 回退值 |

### 5.2 事件类型映射

| AgentSSAS 事件 | AgentMoss 事件 | `subject` | payload 主字段 |
|---|---|---|---|
| `invoke_start` | `chat_request` | `user` | `content` |
| `llm_input` | `model_call` | `action_name` 或 `llm_call` | `messages` |
| `llm_output` | `model_output` | `action_name` 或 `llm_call` | `response` |
| `tool_input` | `tool_call` | `action_name` | `tool_args`、`tool_call_id` |
| `tool_output` | `tool_result` | `action_name` | `tool_result`、`tool_call_id` |
| `invoke_end` | `model_output` | `agent_response` | `response` |

其他事件类型的 `supported` 为 `false`，不会被默认解释为某个已知事件。
分析器会返回 `analysis_status=unsupported_event` 的无风险报告，该报告会被
`report_only_risks` 过滤，不落库、不生成威胁日志。

### 5.3 JSON-like 内容解码

`input_content` 和 `output_content` 可能是字典、列表或 JSON 字符串。适配器会对
以 `{` 或 `[` 开头的字符串递归解码，最多处理 4 层。无法解码的字符串
保持原值，不会因解码失败丢弃事件。

### 5.4 `tool_call_id` 回退规则

对 `tool_input` 和 `tool_output`，适配器按以下顺序构造关联 ID：

1. `event_node.tool_call_id` 或 `aux_ids.tool_call_id`；
2. `event_node.node_id`；
3. `{session_id}:{interaction_seq}:tool:{tool_call_seq}`；
4. 全部缺失时使用空字符串。

这个回退链用于尽可能保留显式关联，但最强契约仍是上报端为同一次
工具调用的输入和输出提供相同的 `tool_call_id`。

### 5.5 `event_id` 回退规则

当 `event_desc.event_id` 缺失时，适配器使用以下素材构造确定性 SHA-256 摘要，
取前 24 个十六进制字符并加上 `ssas-` 前缀：

- 源事件类型；
- `node_id`；
- `session_id`；
- 时间戳；
- 输入内容；
- 输出内容。

这是缺失强 ID 时的幂等补救措施，不应取代上报端生成真实事件 ID。

## 6. 建模输出契约

`adapt_event_desc()` 返回的建模结果同时保留 AgentSSAS 兼容字段和 AgentMoss
专用字段：

```json
{
  "model_type": "agent_behavior_model",
  "adapter_version": "1.0",
  "supported": true,
  "event_type": "tool_input",
  "event_class": "lifecycle",
  "action_name": "bash",
  "input_content": "{\"command\": \"ls\"}",
  "output_content": "",
  "event_id": "event-001",
  "agentmoss_event": {
    "event_type": "tool_call",
    "subject": "bash",
    "payload": {
      "tool_args": {
        "command": "ls"
      },
      "tool_call_id": "call-001"
    },
    "session_id": "session-001",
    "request_id": "trace-001",
    "agent_name": "agent-001",
    "source": "AgentSSASSecurityRail",
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

适配器仅执行结构映射和显式 ID 传递，不会根据自然语言相似度推断
两个事件之间存在数据依赖。

## 7. 分析器状态与事件顺序

### 7.1 输入校验与兼容路径

- 非字典输入：返回 `analysis_status=invalid_model_data`、`confidence=0.0`
  的无风险报告。
- 未包含 `agentmoss_event` 的字典：尝试将其作为旧版 `event_desc`再次适配。
- 不支持的事件：返回 `analysis_status=unsupported_event` 的无风险报告。

这些无风险报告仍是分析器 API 的正常返回值，但在当前 AgentMoss 输出策略下
不会写入结果库或威胁日志。

### 7.2 会话历史隔离

历史键的选择顺序为：

1. 有 `session_id` 时使用 `session:{session_id}`；
2. 无 `session_id` 但有 `request_id` 时使用 `request:{request_id}`；
3. 两者均缺失时使用 `uncorrelated:{event_id}`。

第三种情况不会把缺失关联信息的事件合并到全局历史，从而避免不同
Agent 或不同会话之间的误关联。

### 7.3 顺序和迟到事件

事件以 `(timestamp, event_id)` 排序。分析当前事件时，只选择排序键严格小于
当前事件的已有记录作为历史。当前事件的 `prev_event_ids` 保留最近 5 个
历史事件 ID。

每个会话默认最多保留 200 个事件，实际值不会低于 5。该历史只位于
当前分析器进程内，进程重启后不从 `result.db` 恢复。

### 7.4 幂等与并发

- 报告按 `event_id` 缓存；重复事件直接返回报告的深拷贝，不会重复追加历史。
- 报告缓存上限为 `max_history_events * 4`，超过后按插入顺序移除最早项。
- 历史读取、策略评估、记录写入和报告缓存由可重入锁保护。

缓存可以降低同一进程内重复事件的重复分析，但它不是跨进程幂等保证。

## 8. 单事件规则引擎

### 8.1 分值语义

单事件规则对 `subject + payload JSON` 执行确定性正则匹配。多条规则同时
命中时，当前实现取最高分，不把分值累加。所有命中文本仍会保留在
`findings` 中。

| 类别 | 典型示例 | 当前分值范围 |
|---|---|---:|
| 危险 Shell | `rm -rf`、根目录删除、磁盘覆写、fork bomb、`chmod 777`、下载后执行、强制推送、`sudo` | 70–100 |
| 网络传输能力 | `curl`、`wget`、`scp`、URL、webhook、upload | 30–45 |
| 显式数据外传 | `curl --data`、`wget --post-file`、敏感文件外传、反向 Shell | 85–90 |
| 敏感路径 | `/etc/shadow`、`docker.sock`、`~/.ssh/id_rsa`、`.env`、系统配置 | 78–92 |
| 特权账户操作 | `passwd`、`chpasswd`、密码哈希注入、`userdel` | 82–88 |
| 持久化 | `crontab`、`authorized_keys`、Git hook、systemd service | 82–88 |
| 混淆执行 | Base64 解码后执行、`eval/exec/system` | 82–88 |
| 提示注入和安全绕过 | 忽略旧指令、系统提示词泄漏、关闭监控、jailbreak、脚本注入 | 50–80 |
| 敏感值 | API key、token、password、私钥、信用卡号 | 70 |

### 8.2 工具类型对规则的影响

危险 Shell、账户管理、持久化和混淆执行只在工具名包含 `bash`、`shell`、
`terminal`、`run_command`、`execute` 或 `cmd` 等 Shell 特征时检查。

敏感路径在 Shell 工具或文件访问工具上检查。数据外传和网络传输能力
检查不仅限于 Shell 工具。

### 8.3 自定义敏感模式

`policy.sensitive_patterns` 可以提供自定义的 `name` 和 `pattern`。当自定义列表中
存在可用条目时，它将替换默认敏感模式，而不是与默认模式合并。

## 9. 行为链分析

行为链仅在当前事件是 `tool_call` 时评估，并只查看当前会话最近 30 条
历史。已实现的两条关联规则为：

| 前序信号 | 当前动作 | 分数 | finding |
|---|---|---:|---|
| 历史 `findings` 中存在 `sensitive:` | 当前工具参数命中外传或网络传输模式 | 90 | `behavior chain: sensitive data followed by outbound transfer` |
| 历史 `findings` 中存在 `attempt` | 当前工具参数命中外传或网络传输模式 | 70 | `behavior chain: prompt-injection context followed by outbound transfer` |

行为链规则证明的是“同一会话中出现了特定信号顺序”，不是对任意内容之间
真实数据依赖的形式证明。更强的数据身份证据由 ABG 路径处理。

## 10. ABG 数据泄漏分析

### 10.1 触发条件

ABG 检查只在以下条件同时满足时可能报告数据泄漏：

1. `analyze_agent_behavior_graph=true`；
2. 当前事件是 `tool_call`；
3. 当前动作被分类为 `public_egress`；
4. 当前目标未命中 `agent_behavior_graph_trusted_egress_patterns`；
5. 高机密数据可沿显式数据依赖边到达外传动作节点。

用于单次检查的历史默认最多为 79 条，再加上当前候选事件，总数不超过
`agent_behavior_graph_max_events=80`。

### 10.2 动作分类

| `action_class` | 判定线索 |
|---|---|
| `public_egress` | 配置的外传工具正则，或工具名/参数包含 HTTP 写、upload、webhook、`scp`、`rsync`、`curl` 等线索 |
| `write` | 工具名包含 write、edit、append、save、create_file |
| `read` | 工具名包含 read、cat、grep、search、glob、list_file |
| `compute` | 工具名包含 bash、shell、terminal、python、execute、run_command |
| `other` | 未匹配上述类别 |

分类顺序中 `public_egress` 优先于其他类别。

### 10.3 图节点

| 事件 | 主要节点类型 |
|---|---|
| `tool_call` | `tool_name`、展平后的 `tool_param`、`tool_action`、可选 `resource` |
| `tool_result` | `observation`、可选 `resource` |
| `model_output`、`memory_after_chat` | `llm_response` |
| `system_prompt_build` | `system_prompt` |
| 其他相关事件 | `user_prompt` |

工具参数最多展平 5 层。节点 ID 由 `event_id#node_type[:suffix]` 构成。

### 10.4 安全标签

每个节点包含：

- `confidentiality`：`low` / `medium` / `high`；
- `integrity`：`low` / `medium` / `high`；
- `sensitive_kinds`：敏感数据类型列表。

命中敏感值、高熵密钥或敏感资源时，节点机密性标记为 `high`。
完整性的初始值为：

| 事件类型 | 初始完整性 |
|---|---|
| `system_prompt_build` | high |
| `chat_request`、`memory_before_chat` | high |
| `tool_result`、`memory_after_chat` | low |
| 其他 | medium |

标签只沿 `data_dependency` 边传播。机密性取上界（更高者），完整性取下界
（更低者），敏感类型取并集，直到图达到不动点或迭代次数达到节点数。

### 10.5 依赖边

| 边类型 | 建立依据 | 是否用于机密性传播 |
|---|---|---|
| `control_flow` | 明确的运行时顺序 | 否 |
| `control_dependency` | 声明的前驱 ID、工具选择或低完整性来源控制 | 否 |
| `data_dependency` | 工具参数、资源读写、工具调用 ID、相同敏感值指纹或相同资源 ID | 是 |

具体数据依赖证据包括：

- `tool_param → tool_action`：工具参数驱动工具动作；
- `resource → tool_action`：资源读操作；
- `tool_action → resource`：资源写操作；
- `tool_action → observation`：相同 `tool_call_id` 的工具输入和输出；
- 早期节点到后期参数/资源节点：相同敏感值指纹或相同资源身份。

同一会话中“先出现 A，后出现 B”只会形成控制流边，不会自动形成
数据依赖边。

### 10.6 ABG 违规

| `rule_id` | 触发特征 | 风险分 |
|---|---|---:|
| `agent-behavior-graph-sensitive-data-staging-to-public-egress` | 高机密数据经历历史写操作暂存后进入公共外传 | 96 |
| `agent-behavior-graph-sensitive-resource-to-public-egress` | 敏感资源身份的数据进入公共外传 | 95 |
| `agent-behavior-graph-low-integrity-confidential-egress` | 低完整性来源控制机密数据外传 | 95 |
| `agent-behavior-graph-high-confidentiality-to-public-egress` | 其他已证明的高机密数据公共外传 | 95 |

检查证据保留目标动作节点和可达上游数据节点 ID。上游反向遍历仅沿
`data_dependency` 边进行，最多访问 200 个节点。

### 10.7 证据脱敏

ABG 节点不保存命中的敏感明文，而是保存：

- 敏感类型；
- 资源标识；
- 敏感值规范化后 SHA-256 摘要的前 24 个十六进制字符。

这个脱敏范围只适用于 ABG 证据。AgentSSAS 原始事件库是否含明文，取决于
上游采集和存储策略，不由 ABG 证据脱敏替代。

## 11. 风险、等级与建议决策

### 11.1 三个不同概念

| 概念 | 来源 | 含义 |
|---|---|---|
| `risk_score` / `risk_level` | 单事件、行为链和 ABG 的最高检测分 | 检测到的风险强度 |
| `has_risk` | `risk_score` 与 `risk_threshold` 比较 | 该结果是否被 AgentMoss 视为风险报告 |
| `decision` | `enforcement_scope` 选中的分数与 ask/block 阈值比较 | AgentMoss 建议，不是 AgentSSAS 已执行动作 |

这三者必须分开理解。例如，默认 `enforcement_scope=behavior_chain`：

- `rm -rf /tmp/demo` 可以命中 82 分单事件规则；
- 报告为 `has_risk=true`、`risk_level=high`；
- 但单事件信号不参与默认建议决策，因此 `decision=allow`；
- 这不代表操作安全，也不代表 AgentSSAS 已经放行或阻断。

### 11.2 风险等级

| 分数 | `risk_level` |
|---:|---|
| 0 | `safe` |
| 1–44 | `low` |
| 45–69 | `medium` |
| 70–89 | `high` |
| 90–100 | `critical` |

`has_risk` 的实际判定为：

```text
risk_score > 0 and risk_score >= threshold_score(risk_threshold)
```

`risk_threshold` 支持 `safe`、`low`、`medium`、`high`、`critical` 或数值。
未识别的字符串回退到 1 分。

### 11.3 建议决策

`enforcement_scope` 控制哪些分数参与 `decision` 生成：

| 值 | 建议决策所使用的分数 |
|---|---|
| `all` | 单事件分数与已启用行为链/ABG 分数的最大值 |
| `behavior_chain` | 只使用已启用的行为链/ABG 分数 |
| `observe` | 强制使用 0 分，因此返回 `default_decision` |

别名 `atomic`、`atomic_and_chain`、`full` 映射到 `all`；`chain`、`chain_only`
映射到 `behavior_chain`；`analysis`、`record`、`none` 等映射到 `observe`。

默认阈值为：

- 分数 `>= 80`：`block`；
- 否则分数 `>= 45`：`ask`；
- 否则：`default_decision`，默认为 `allow`。

报告中的建议动作映射为：

| `decision` / 风险 | `recommended_actions` |
|---|---|
| `block` | `block`、`investigate` |
| `ask` | `review`、`ask_user` |
| 其他且 `has_risk=true` | `investigate`、`log` |
| 无风险 | `log` |

## 12. AgentSSAS 报告契约

### 12.1 主要字段

| 字段 | 语义 |
|---|---|
| `has_risk` | 是否达到 AgentMoss 风险阈值 |
| `risk_level` | 由风险分映射的五级等级 |
| `risk_type` | `detected_threats` 中的第一项，无命中时为空 |
| `risk_score` | 0–100 的当前最高分 |
| `confidence` | 已分析报告当前固定为 1.0 |
| `detected_threats` | 根据 finding 文本映射的去重风险类型 |
| `recommended_actions` | AgentMoss 建议动作 |
| `analytic_name` | `AgentMoss Runtime Behavior Analysis` |
| `description` | 命中数量和建议决策的简述 |
| `module_name` | 固定为 `agent_moss` |

`evidence` 包含：

| 路径 | 语义 |
|---|---|
| `analysis_status` | `analyzed`、`invalid_model_data` 或 `unsupported_event` |
| `decision` | AgentMoss 建议：`allow`、`ask` 或 `block` |
| `decision_mode` | 固定为 `advisory_notify` |
| `findings` | 原始确定性规则命中 |
| `event_mapping` | 源事件、AgentMoss 事件和适配器版本 |
| `correlation` | 追踪、会话、序号、工具调用和节点 ID |
| `history_events_analyzed` | 本次分析使用的前序事件数 |
| `agent_behavior_graph` | 仅命中 ABG finding 时存在，包含违规及可选脱敏图 |

### 12.2 风险报告示例

```json
{
  "has_risk": true,
  "risk_level": "high",
  "risk_type": "dangerous_tool_operation",
  "risk_score": 82.0,
  "confidence": 1.0,
  "detected_threats": [
    "dangerous_tool_operation"
  ],
  "recommended_actions": [
    "investigate",
    "log"
  ],
  "analytic_name": "AgentMoss Runtime Behavior Analysis",
  "description": "AgentMoss detected 1 behavior signal(s); decision=allow",
  "evidence": {
    "analysis_status": "analyzed",
    "decision": "allow",
    "decision_mode": "advisory_notify",
    "findings": [
      "recursive force remove"
    ],
    "event_mapping": {
      "source_event_type": "tool_input",
      "agentmoss_event_type": "tool_call",
      "adapter_version": "1.0"
    },
    "correlation": {
      "trace_id": "trace-001",
      "session_id": "session-001",
      "interaction_seq": 0,
      "llm_call_seq": 0,
      "tool_call_seq": 0,
      "tool_call_id": "call-001",
      "node_id": "session-001_0_toolcall_0"
    },
    "history_events_analyzed": 0
  },
  "module_name": "agent_moss"
}
```

AgentSSAS 流水线还会在报告缺失时补入 `aux_ids`、`event_node`、
`recommended_actions`、`module_name` 和 `analytic_type_id`，用于模块存储与 OCSF 呈现。

## 13. 仅风险上报语义

AgentMoss 在 `module.yaml` 中配置：

```yaml
report_only_risks: true
```

该选项的精确语义是：

| 阶段 | `has_risk=false` | `has_risk=true` |
|---|---|---|
| 事件适配 | 执行 | 执行 |
| 单事件/行为链/ABG 分析 | 执行 | 执行 |
| 分析器会话历史更新 | 执行 | 执行 |
| 返回报告给流水线聚合层 | 执行 | 执行 |
| `modules/agent_moss/result.db` | 不写入 | 写入 |
| OCSF 威胁日志 | 不生成 | 生成 |
| `ssas_core.db` 的 `alerts` 表 | 不写入 | 风险等级高于 safe 时写入 |

需要注意：AgentSSAS 的原始事件存储与 AgentMoss 分析报告存储是两个概念。
`report_only_risks=true` 不会禁止 AgentSSAS 核心库保存原始事件。

其他检测模块未开启该选项时，仍保持“所有分析报告进入模块结果库和
呈现层”的既有行为。

## 14. 存储和生命周期

默认存储根目录为 `<ssas_home>/ssas/`：

```text
<ssas_home>/ssas/
├── ssas_core.db
│   ├── raw_events        # AgentSSAS 原始事件
│   ├── events            # AgentSSAS 统一事件
│   └── alerts            # 有风险的模块报告
├── modules/agent_moss/
│   ├── process.db
│   └── result.db        # 当前仅 has_risk=true 的 AgentMoss 报告
└── reports/threat_log/
    └── *.json               # 当前仅 AgentMoss 风险报告生成的 OCSF 日志
```

AgentMoss 会话历史和报告缓存是进程内有界状态，不持久化，也不在重启后
从数据库回放。因此，进程重启后的首批事件可能缺少重启前的行为链上下文。

## 15. 配置说明

### 15.1 模块顶层配置

| 配置 | 当前值 | 说明 |
|---|---:|---|
| `enabled` | `true` | 加载 AgentMoss 模块 |
| `event_version` | `1.0` | AgentSSAS 事件格式版本 |
| `analytic_type_id` | `2` | OCSF 分析类型映射为 Behavior |
| `report_only_risks` | `true` | 安全结果不写入模块结果库和 OCSF 日志 |
| `subscribed_events` | 6 类生命周期事件 | 均为默认 notify 订阅 |

### 15.2 分析器配置

| 配置 | 当前值 | 说明 |
|---|---:|---|
| `analysis_methods` | `[rule, behavior_chain, agent_behavior_graph]` | 能力声明；当前代码只保存此列表，实际开关由 `policy` 中的 `analyze_*` 配置控制 |
| `risk_threshold` | `low` | 生成 `has_risk=true` 的最低分值 |
| `max_history_events` | `200` | 每个历史键保留的最大事件数 |
| `include_agent_behavior_graph` | `true` | ABG 命中时在证据中加入脱敏图 |

### 15.3 策略配置

| 配置 | 当前值 | 说明 |
|---|---:|---|
| `default_decision` | `allow` | 未达到 ask/block 阈值时的建议 |
| `enforcement_scope` | `behavior_chain` | 只让行为链和已启用 ABG 分数影响建议决策 |
| `ask_threshold` | `45` | 建议询问的最低分 |
| `block_threshold` | `80` | 建议阻断的最低分 |
| `enforce_behavior_chain` | `true` | 行为链分数可参与建议决策 |
| `analyze_behavior_chain` | `true` | 执行行为链规则 |
| `analyze_agent_behavior_graph` | `true` | 构建并检查 ABG |
| `enforce_agent_behavior_graph` | `true` | ABG 风险分可参与建议决策 |
| `agent_behavior_graph_max_events` | `80` | 单次 ABG 候选与历史事件总上限 |
| `agent_behavior_graph_trusted_egress_patterns` | `[]` | 可信外传目标正则，命中后不报 ABG 外传违规 |
| `agent_behavior_graph_egress_tool_patterns` | `[]` | 补充公共外传工具名正则 |
| `analyze_destructive_shell` | `true` | 危险 Shell 检测 |
| `analyze_sensitive_path_access` | `true` | 敏感路径检测 |
| `analyze_data_exfiltration` | `true` | 显式数据外传检测 |
| `analyze_privileged_account_ops` | `true` | 特权账户操作检测 |
| `analyze_persistence_ops` | `true` | 持久化检测 |
| `analyze_obfuscated_execution` | `true` | 混淆执行检测 |

## 16. 典型场景

### 16.1 安全工具调用

```json
{
  "tool_name": "bash",
  "tool_args": {
    "command": "ls"
  }
}
```

分析结果为 `risk_score=0`、`risk_level=safe`、`has_risk=false`。事件进入
会话历史，但不写入 AgentMoss `result.db`，不生成 AgentMoss OCSF 威胁日志或告警。

### 16.2 单事件危险 Shell

```json
{
  "tool_name": "bash",
  "tool_args": {
    "command": "rm -rf /tmp/demo"
  }
}
```

默认配置下命中 `recursive force remove`，风险分至少为 82，报告为
`has_risk=true`、`risk_level=high`。因 `enforcement_scope=behavior_chain`，
建议决策仍可为 `allow`，但报告会正常落库和上报。

### 16.3 敏感数据显式流向公共外传

一个具有强证据的最小链路是：

```text
tool_output(read_file, tool_call_id=read-1)
  payload 包含敏感值 S
        ↓ 相同敏感值指纹
tool_input(send_http, tool_call_id=send-1)
  body 中包含同一敏感值 S
        ↓
public_egress
```

此时 ABG 建立从观测节点到外传参数、再到外传动作的数据依赖路径。
对工具结果这类低完整性来源，当前实现会命中
`agent-behavior-graph-low-integrity-confidential-egress`，风险分 95，默认建议为 `block`。

## 17. 异常处理与失效模式

| 情况 | 当前行为 |
|---|---|
| 建模插件抛异常 | 流水线记录异常，跳过 AgentMoss，不影响其他模块 |
| 分析插件抛异常 | 流水线记录异常，跳过 AgentMoss |
| 分析结果非字典 | 流水线忽略该结果 |
| 模块结果存储失败 | 记录异常，仍尝试 OCSF 呈现和报告聚合 |
| OCSF 呈现失败 | 记录异常，仍返回已生成的报告 |
| 告警写入主库失败 | 记录异常，不反向影响分析和主流程 |

当前整体策略倾向 fail-open。这保证了态势感知模块故障不会意外中断
Agent 主业务，但也意味着本模块不提供“分析故障时强制阻断”保障。

## 18. 证据边界与已知限制

### 18.1 可以直接声明的能力

- 可以证明某个具体事件命中了已实现的确定性规则。
- 可以证明同一会话的近期历史中出现了特定行为链信号。
- 当存在相同敏感值指纹、相同资源 ID 或匹配工具调用 ID 时，可以构造
  对应的 ABG 数据依赖证据。

### 18.2 不应夸大的能力

以下情况不代表已经证明安全：

- 事件未命中当前正则规则；
- 敏感数据被未识别的编码、压缩、加密或语义转换；
- 数据依赖只有自然语言相似，没有指纹、资源或调用 ID 证据；
- 必需的生命周期 hook、payload 或关联 ID 没有上报；
- 前序事件已超出行为链或 ABG 历史窗口；
- 进程重启造成内存历史丢失；
- `notify` 报告中出现 `block` 或 `ask`。

这些情况应表达为“在当前观测与证据边界下未证明风险”或 `unknown`，
不能表达为完整系统的安全证明。

## 19. 开发与验证

### 19.1 核心测试场景

当前测试覆盖：

- 6 类 AgentSSAS 生命周期事件映射；
- `tool_call_id` 显式保留与 `node_id` 回退；
- 非字典输入和不支持事件；
- 安全工具调用；
- 危险 Shell、敏感路径、持久化、特权账户与混淆执行；
- 显式敏感值流向公共外传的 ABG 违规；
- ABG 证据不包含敏感明文；
- 会话历史隔离；
- 危险结果上报与安全结果不上报。

### 19.2 验证命令

在 AgentSSAS 项目目录执行：

```bash
PYTHONPATH=src <python> -m pytest \
  tests/agent_ssas/core/detection_modules/test_agent_moss.py -q

PYTHONPATH=src <python> -m pytest \
  tests/agent_ssas/core/integration/test_pipeline_integration.py -q

PYTHONPATH=src <python> -m pytest tests -q
```

### 19.3 更新引擎快照

更新 `engine/` 时应：

1. 确认新的上游提交；
2. 对比 `events.py`、`policy.py`、`agent_behavior_graph.py`；
3. 仅修改包内相对导入等 AgentSSAS 嵌入必需项；
4. 更新 `engine/SOURCE.md` 的来源与哈希；
5. 重新运行事件映射、行为链、ABG、脱敏、会话隔离和输出策略测试。

## 20. 运维排查

### 20.1 风险事件没有生成 AgentMoss 告警

依次检查：

1. AgentMoss 模块是否启用；
2. 事件是否属于 6 类已订阅生命周期事件；
3. `event_node.action_name`、`input_content` 和 `output_content` 是否完整；
4. `risk_threshold` 是否高于实际风险分；
5. 后台 notify 任务是否已完成；
6. `modules/agent_moss/result.db`、`reports/threat_log/` 和主库 `alerts` 表是否可写；
7. 运行日志中是否存在建模、分析、存储或呈现异常。

### 20.2 安全事件在 `result.db` 中查不到

这是当前的预期行为。`report_only_risks=true` 使 AgentMoss 只持久化
`has_risk=true` 的分析报告。如果需要审计安全事件，应查询 AgentSSAS 原始事件库，
或在明确接受额外存储量后关闭该选项。

### 20.3 报告中是 `block`，Agent 却继续执行

这是 `notify` 订阅的预期语义。当前 AgentMoss 只输出建议决策，没有接入
JiuwenSwarm 操作前的同步强制执行点。要提供运行时阻断，需要单独设计 auth 订阅、
超时策略、决策消费和工具执行前门控链路，不能仅依赖当前报告字段。

## 21. 相关文件

- [`README.md`](../src/agent_ssas/core/detection_modules/agent_moss/README.md)：模块概览。
- [`module.yaml`](../src/agent_ssas/core/detection_modules/agent_moss/module.yaml)：当前模块配置。
- [`event_adapter.py`](../src/agent_ssas/core/detection_modules/agent_moss/event_adapter.py)：事件适配实现。
- [`agent_moss_analyzer.py`](../src/agent_ssas/core/detection_modules/agent_moss/agent_moss_analyzer.py)：分析器与报告映射。
- [`policy.py`](../src/agent_ssas/core/detection_modules/agent_moss/engine/policy.py)：单事件和行为链策略。
- [`agent_behavior_graph.py`](../src/agent_ssas/core/detection_modules/agent_moss/engine/agent_behavior_graph.py)：ABG 建模与检查。
- [`SOURCE.md`](../src/agent_ssas/core/detection_modules/agent_moss/engine/SOURCE.md)：引擎快照来源。
