# 检测模块参考

检测模块（detection module）是 AgentSSASCore 威胁检测的执行单元。每个模块由一个 `module.yaml` 声明，包含一个数据建模插件与一个威胁分析插件，并拥有独立的存储目录。模块由 `DetectionModuleManager`（`src/agent_ssas/core/framework/module_manager/manager.py`）在启动时扫描加载。

相关文档：

- [事件格式参考](./event-format.md)：模块订阅的事件类型定义
- [启用与禁用检测模块](../how-to/enable-disable-detection-modules.md)：开关模块的操作步骤
- [配置参考](./configuration.md)：`ssas.modules` 段说明

## 模块加载机制

`DetectionModuleManager.initialize()` 在启动时扫描 `agent_ssas/core/detection_modules/` 目录：

- 遍历每个子目录（跳过 `_` 前缀目录与 `common` 通用组件目录），读取其中的 `module.yaml`。
- 校验必需字段（`name`、`data_modeling` 插件、`threat_analysis` 插件），缺失时抛出异常。
- 支持通过 config.yaml 的 `ssas.modules.<模块名>.enabled` 覆盖 `module.yaml` 的 `enabled` 字段；`enabled` 为 false 的模块直接跳过。
- 动态导入并实例化插件：先在框架预置插件目录（`agent_ssas.core.framework.data_modeler.plugins` / `agent_ssas.core.framework.threat_analyzer.plugins`）中查找插件类，未找到再按类名转 snake_case 推导的文件名到模块目录（`agent_ssas.core.detection_modules.<模块名>.`）中查找。实例化时优先传入插件专有配置（`config` 字段），构造函数不接受参数时回退到无参实例化。
- 为每个模块创建独立的存储管理器（`process.db`、`result.db`、`config/` 目录）。
- 根据 `subscribed_events` 构建订阅表。单个模块加载失败时记录异常并跳过，不影响其他模块。

## module.yaml 字段参考

```yaml
name: <模块名>                    # 必需,唯一标识,同时用作存储子目录名
display_name: "<显示名>"          # 可选,缺省同 name
enabled: true                     # 可选,默认 true;可被 config.yaml ssas.modules.<name>.enabled 覆盖
event_version: "1.0"              # 可选,事件格式版本,默认 "1.0"
subscribed_events:                # 可选,默认 ["*"]
  - "tool_input"                  #   基础事件名(notify 模式)
  - "permission_interrupt_tool:auth"  #   :auth 后缀声明 auth 模式
analytic_type_id: 1               # 可选,分析类型 ID(OCSF 映射: 1=Rule, 2=Behavior),默认 0
auth_timeout_policy: allow        # 可选,auth 订阅者超时后的默认策略,默认 "allow"

plugins:                          # 必需,须同时包含以下两类插件
  - type: data_modeling           # 数据建模插件
    name: <插件类名>              #   在框架预置目录或模块目录中查找
    model_type: <模型类型>        #   产出的数据模型类型
  - type: threat_analysis         # 威胁分析插件
    name: <插件类名>
    expected_model_type: <模型类型>  # 消费的数据模型类型(与建模插件产出匹配)
    config:                       # 可选,插件专有配置,实例化时传给插件构造函数
      ...: ...
```

字段说明：

| 字段 | 必需 | 说明 |
|------|------|------|
| `name` | 是 | 模块唯一标识 |
| `display_name` | 否 | 展示名，缺省同 `name` |
| `enabled` | 否 | 启用开关，默认 `true` |
| `event_version` | 否 | 事件格式版本号，默认 `"1.0"` |
| `subscribed_events` | 否 | 订阅事件列表，默认 `["*"]` |
| `analytic_type_id` | 否 | 分析类型 ID，用于 OCSF 报告的 `analytic.type` 映射：1=Rule、2=Behavior，默认 0 |
| `auth_timeout_policy` | 否 | auth 模式订阅者超时后的默认策略，默认 `allow` |
| `plugins` | 是 | 插件列表，必须同时包含一个 `data_modeling` 与一个 `threat_analysis` 插件 |

插件类型：

- `data_modeling`（数据建模插件）：消费事件，产出指定 `model_type` 的数据模型（如 `agent_behavior_model`、`blank`）。声明字段为 `name`（插件类名）与 `model_type`（模型类型）。
- `threat_analysis`（威胁分析插件）：消费 `expected_model_type` 类型的模型，输出威胁分析报告。声明字段为 `name`、`expected_model_type` 与可选的 `config`（插件专有配置字典，实例化时注入）。

## 订阅模式语法

`subscribed_events` 中每项是一个事件规格字符串，语法为 `事件名[:模式]`：

| 写法 | 模式 | 说明 |
|------|------|------|
| `"tool_input"` | notify（默认） | 后台异步检测，`report_event` 不等待结果，不阻断运行时 |
| `"tool_input:auth"` | auth | 同步检测，`report_event` 必须等待结果，参与运行时决策 |
| `"*"` | 恒为 notify | 通配订阅全部事件（无法用 `:auth` 声明 auth 模式） |

另支持聚合事件订阅，管理器将其展开为关联的基础事件：

| 聚合事件 | 展开为基础事件 | auth 作用事件 |
|---------|--------------|--------------|
| `one_toolcall_event` | tool_input、tool_output | tool_output |
| `one_llmcall_event` | llm_input、llm_output、tool_input、tool_output | llm_output |
| `one_interaction_event` | invoke_start、invoke_end、llm_input、llm_output、tool_input、tool_output | invoke_end |

auth 模式只作用在聚合事件的结束事件上，其余展开事件均为 notify。

当前 3 个内置模块均以 notify 模式订阅。

## 内置模块

### agent_moss

AgentMoss 行为链与 PDG 检测模块，默认启用。完整 `module.yaml`（`src/agent_ssas/core/detection_modules/agent_moss/module.yaml`）：

```yaml
name: agent_moss
display_name: "AgentMoss 行为链与 PDG 检测模块"
enabled: true
event_version: "1.0"
subscribed_events:
  - "invoke_start"
  - "llm_input"
  - "llm_output"
  - "tool_input"
  - "tool_output"
  - "invoke_end"
analytic_type_id: 2

plugins:
  - type: data_modeling
    name: AgentMossModeler
    model_type: agent_behavior_model
  - type: threat_analysis
    name: AgentMossAnalyzer
    expected_model_type: agent_behavior_model
    config:
      analysis_methods: [rule, behavior_chain, pdg]
      risk_threshold: low
      max_history_events: 200
      include_pdg_graph: true
      policy:
        default_decision: allow
        # AgentSSAS subscribes in notify mode. Decisions are reported as
        # recommendations and do not block the JiuwenSwarm runtime.
        enforcement_scope: behavior_chain
        ask_threshold: 45
        block_threshold: 80
        enforce_behavior_chain: true
        analyze_behavior_chain: true
        analyze_pdg_data_leakage: true
        enforce_pdg_data_leakage: true
        pdg_max_events: 80
        pdg_trusted_egress_patterns: []
        pdg_egress_tool_patterns: []
        analyze_destructive_shell: true
        analyze_sensitive_path_access: true
        analyze_data_exfiltration: true
        analyze_privileged_account_ops: true
        analyze_persistence_ops: true
        analyze_obfuscated_execution: true
```

行为说明：

- 订阅全部 6 个生命周期事件（notify 模式），通过结构适配层转换为 AgentMoss 事件（invoke_start→chat_request、llm_input→model_call、llm_output/invoke_end→model_output、tool_input→tool_call、tool_output→tool_result），保留 trace_id、session_id、各序号与 tool_call_id。
- `analysis_methods` 声明三类分析方法：`rule`（确定性规则）、`behavior_chain`（行为链分析）、`pdg`（程序依赖图数据泄露分析）。
- `policy` 段为 AgentMoss 分析引擎的运行参数，关键项含义：
  - `default_decision`：无风险时的默认决策。
  - `ask_threshold` / `block_threshold`：风险分数达到阈值时分别产生 ask（询问）与 block（阻断）建议。
  - `enforcement_scope`：强制执行的分析范围（behavior_chain）。
  - `analyze_*` 系列：各检测维度的开关（行为链、PDG 数据泄露、破坏性 shell、敏感路径访问、数据外泄、特权账户操作、持久化操作、混淆执行）。
  - `enforce_behavior_chain` / `enforce_pdg_data_leakage`：对应维度的强制执行开关。
  - `pdg_max_events`：PDG 分析的最大历史事件数。
  - `pdg_trusted_egress_patterns` / `pdg_egress_tool_patterns`：可信出口模式与出口工具模式白名单。
  - `max_history_events`：按 session 维护的有界历史上限。
- 模块以 notify 模式运行：风险结果、建议动作与脱敏 PDG 证据写入 `modules/agent_moss/result.db` 及威胁日志，决策以建议形式上报（`advisory_notify`），不直接阻断 JiuwenSwarm。
- `analytic_type_id: 2` 在 OCSF 报告中映射为 `analytic.type = "Behavior"`。

### security_rail_detection

安全护栏检测模块，默认启用。完整 `module.yaml`（`src/agent_ssas/core/detection_modules/security_rail_detection/module.yaml`）：

```yaml
name: security_rail_detection
display_name: "安全护栏检测模块"
enabled: true
event_version: "1.0"
subscribed_events:
  - "permission_interrupt_tool"    # notify 模式 (默认), 后台异步检测, 不参与决策阻断
analytic_type_id: 1

plugins:
  - type: data_modeling
    name: BlankDataModeler
    model_type: blank
  - type: threat_analysis
    name: SecurityRailAnalyzer
    expected_model_type: blank
```

行为说明：

- 订阅 `permission_interrupt_tool` 安全检测衍生事件（notify 模式），消费事件 payload 中的 `risk_source`、`risk_type`、`risk_level`、`decision`、`evidence` 字段。
- 数据建模插件为 `BlankDataModeler`（空白建模，不产生模型内容），威胁分析插件为 `SecurityRailAnalyzer`（位于模块目录 `security_rail_analyzer.py`），将其他安全 Rail 的拦截决策聚合为风险评估结果并写入自身 result.db 与威胁日志。
- `analytic_type_id: 1` 在 OCSF 报告中映射为 `analytic.type = "Rule"`。

### test_detection

测试检测模块，默认禁用。完整 `module.yaml`（`src/agent_ssas/core/detection_modules/test_detection/module.yaml`）：

```yaml
name: test_detection
display_name: "测试检测模块"
enabled: false
event_version: "1.0"
subscribed_events: ["*"]
analytic_type_id: 1

plugins:
  - type: data_modeling
    name: BlankDataModeler
    model_type: blank
  - type: threat_analysis
    name: BlankThreatAnalyzer
    expected_model_type: blank
```

行为说明：

- 用途：验证模块管理器的加载、订阅与存储机制。通配订阅 `*`（全部事件，notify 模式），插件均为空白实现（`BlankDataModeler` / `BlankThreatAnalyzer`，位于框架预置插件目录），不产生实际检测结果。
- 默认 `enabled: false`，需要验证模块机制时可按[启用与禁用检测模块](../how-to/enable-disable-detection-modules.md)开启。

## 模块存储结构

每个成功加载的模块获得独立存储目录（由 `ModuleStorageManager` 管理）：

```text
<storage>/modules/<模块名>/
├── process.db    # 过程数据(建模数据、中间结果)
├── result.db     # 结果数据(告警、审计、威胁分析报告)
└── config/       # 配置数据(检测规则、基线、阈值参数,手动更新)
```

过程与结果分库，便于按不同 TTL（`event_ttl_days` / `alert_ttl_days`）独立清理。查询方式见[查看威胁日志](../how-to/view-threat-logs.md)。

## 相关文档

- [事件格式参考](./event-format.md)
- [配置参考](./configuration.md)
- [启用与禁用检测模块](../how-to/enable-disable-detection-modules.md)
- [切换决策策略](../how-to/switch-decision-policy.md)
