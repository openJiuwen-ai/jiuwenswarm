# 查看威胁日志

AgentSSASCore 的呈现层会把每个威胁分析报告构建为 OCSF（Open Cybersecurity Schema Framework）Detection Finding 格式的 JSON 文件落盘。本文说明日志的位置、命名规则、字段含义，以及如何查询 SQLite 存储中的原始事件与检测结果。

## 落盘位置

威胁日志位于存储目录下：

```text
<storage>/reports/threat_log/*.json
```

其中 `<storage>` 为 `AgentSSASConfig.storage_path`（即 `<ssas_home>/ssas`）。默认情况下是：

```text
~/.jiuwenswarm/ssas/reports/threat_log/*.json
```

存储根目录按 `SSAS_HOME` > `JIUWENSWARM_DATA_DIR` > `JIUWENSWARM_HOME` > `~/.jiuwenswarm` 的优先级解析，详见[配置参考](../reference/configuration.md)。

## 文件命名规则

文件名格式为：

```text
threat_{trace_id}_{module_name}_{YYYYMMDD_HHMMSS_mmm}.json
```

- `trace_id`：产生该报告的事件所属的链路追踪 ID；缺失时为 `notrace`。
- `module_name`：产出报告的检测模块名（如 `agent_moss`、`security_rail_detection`）；缺失时为 `unknown`。
- `YYYYMMDD_HHMMSS_mmm`：落盘时刻的本地时区可读时间，毫秒部分避免同一秒内多个报告相互覆盖。

示例：

```text
threat_demo-trace_agent_moss_20260904_165015_475.json
threat_demo-trace_security_rail_detection_20260904_165015_489.json
```

文件内容为 UTF-8 编码、缩进 2 空格的 JSON，中文原样保留（`ensure_ascii=false`）。

## OCSF 字段说明

报告基于 OCSF 1.8.0+ 的 Detection Finding 事件类（`class_uid=2004`）并声明 `ai_operation` profile。顶层字段分为三部分：框架固定字段（从事件数据自动构建）、检测模块固定格式字段（映射到 `finding_info`）、检测模块自定义字段（`evidences` 数组承载）。

### 顶层固定字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `activity_id` / `activity_name` | int / str | 固定为 `1` / `Create` |
| `category_uid` / `category_name` | int / str | 固定为 `2` / `Findings` |
| `class_uid` / `class_name` | int / str | 固定为 `2004` / `Detection Finding` |
| `severity_id` | int | 风险等级映射：safe=1(Info)、low=2、medium=3、high=4、critical=5 |
| `status` / `status_id` | str / int | 固定为 `New` / `1`（新创建的检测发现） |
| `time` | int | 事件时间戳（毫秒） |
| `trace_id` | str | 链路追踪 ID |
| `actor` | object | 智能体主体：`name` 为 agent_id，`type_id=4`、`type="Application"` |
| `metadata` | object | 产品元信息，见下文 |
| `finding_info` | object | 检测发现详情，见下文 |
| `evidences` | array | 证据工件（OCSF 标准 Evidence Artifacts），见下文 |
| `ai_agent` | object | 智能体信息（ai_operation profile），见下文 |
| `message_context` | object | 模型输入输出上下文（ai_operation profile），见下文 |
| `ai_operation` | object | 交互层次结构（ai_operation profile），见下文 |

### metadata

```json
{
  "product": {
    "name": "AgentSSAS",
    "vendor_name": "AgentSSAS",
    "feature": { "name": "<检测模块名>" }
  },
  "version": "1.0",
  "profiles": ["ai_operation"]
}
```

`metadata.product.feature.name` 承载第二级发现者（检测模块名）。

### finding_info

| 字段 | 类型 | 说明 |
|------|------|------|
| `uid` | str | 检测发现的唯一标识（UUID） |
| `desc` | str | 检测模块输出的报告描述 |
| `created_time` | int | 报告创建时间（毫秒时间戳） |
| `created_time_dt` | str | 报告创建时间（ISO 8601，UTC） |
| `confidence_id` | int | 置信度档位：0=Unknown、1=Low、2=Medium、3=High |
| `confidence` | str | 置信度档位名称，与 `confidence_id` 对应 |
| `confidence_score` | int | 置信度分值（0-100），由 `confidence`（0-1）×100 换算（`risk_score` 不占用此字段） |
| `types` | list[str] | 风险类型列表，取自 `RiskAssessment.risk_type` |
| `analytic` | object | 检测策略信息：`type_id` 来自 module.yaml 的 `analytic_type_id`（1=Rule、2=Behavior），`name` 为检测模块输出的策略名 |

### evidences

`evidences` 为数组，每项是一个证据工件。框架将 `detected_threats`、`recommended_actions` 与检测模块自定义 `evidence` 合并到 `data` 字段：

```json
[
  {
    "uid": "<UUID>",
    "name": "detection_evidence",
    "data": {
      "detected_threats": [],
      "recommended_actions": ["log"],
      "...": "检测模块自定义证据字段（如 decision、findings、correlation 等）"
    }
  }
]
```

注意：`data` 序列化后的 JSON 长度上限为 65536 字符，超出时会添加 `_truncated: true` 标记。

### ai_agent 与 message_context

- `ai_agent`：`uid`/`name` 为 agent_id，`instance_uid` 为 session_id，`type_id=1`、`type="Native"`。
- `message_context`：`ai_role_id`/`ai_role` 按事件类型映射（invoke_start/invoke_end 为 User，llm_input/llm_output 为 Assistant，工具相关事件为 Tool）；`prompt_text` 与 `response_text` 分别保存模型输入与输出（工具的输入输出不在其中，而在 `ai_operation` 中）；`application.name` 固定为 `JiuwenSwarm`。

### ai_operation

按 interaction → llm_call → tool_call 层次组织事件内容：

```json
{
  "interactions": [
    {
      "seq": 0,
      "input": "<invoke_start 的 query>",
      "output": "<invoke_end 的 result>",
      "llm_calls": [
        {
          "seq": 0,
          "input": "<llm_input 的 prompt>",
          "output": "<llm_output 的 response>",
          "tool_calls": [
            {
              "seq": 0,
              "name": "<tool_name>",
              "input": "<tool_args>",
              "output": "<tool_result>",
              "actions": []
            }
          ]
        }
      ]
    }
  ]
}
```

当前事件按其类型与序号填充到对应层级，上层结构以空壳占位保持层次完整；不具备的序号字段（`-1`）同样原样保留。

## 完整示例

以下是一份实际的威胁日志（`agent_moss` 模块、invoke_start 事件、未检出风险）：

```json
{
  "activity_id": 1,
  "activity_name": "Create",
  "category_uid": 2,
  "category_name": "Findings",
  "class_uid": 2004,
  "class_name": "Detection Finding",
  "severity_id": 1,
  "status": "New",
  "status_id": 1,
  "time": 1788511815475,
  "trace_id": "demo-trace",
  "actor": {
    "name": "demo-agent",
    "type_id": 4,
    "type": "Application"
  },
  "metadata": {
    "product": {
      "name": "AgentSSAS",
      "vendor_name": "AgentSSAS",
      "feature": {
        "name": "agent_moss"
      }
    },
    "version": "1.0",
    "profiles": [
      "ai_operation"
    ]
  },
  "finding_info": {
    "uid": "da2b0b81-f430-40c6-8a43-aace6ed3a624",
    "desc": "AgentMoss found no behavior risk",
    "created_time": 1788511815475,
    "created_time_dt": "2026-09-04T08:50:15.000Z",
    "confidence_id": 3,
    "confidence": "High",
    "confidence_score": 100,
    "types": [],
    "analytic": {
      "type_id": 2,
      "type": "Behavior",
      "name": "AgentMoss Runtime Behavior Analysis"
    }
  },
  "evidences": [
    {
      "uid": "92915c0b-fa18-43dc-853f-a661119ede1e",
      "name": "detection_evidence",
      "data": {
        "recommended_actions": [
          "log"
        ],
        "analysis_status": "analyzed",
        "decision": "allow",
        "decision_mode": "advisory_notify",
        "findings": [],
        "event_mapping": {
          "source_event_type": "invoke_start",
          "agentmoss_event_type": "chat_request",
          "adapter_version": "1.0"
        },
        "correlation": {
          "trace_id": "demo-trace",
          "session_id": "demo-session",
          "interaction_seq": 0,
          "llm_call_seq": -1,
          "tool_call_seq": -1,
          "tool_call_id": "",
          "node_id": "demo-session_0_interaction"
        },
        "history_events_analyzed": 0
      }
    }
  ],
  "ai_agent": {
    "uid": "demo-agent",
    "name": "demo-agent",
    "type_id": 1,
    "type": "Native",
    "instance_uid": "demo-session"
  },
  "message_context": {
    "ai_role_id": 1,
    "ai_role": "User",
    "prompt_text": "帮我列出当前目录下的文件",
    "response_text": "",
    "application": {
      "name": "JiuwenSwarm"
    },
    "service": {
      "name": "AgentSSASSecurityRail"
    }
  },
  "ai_operation": {
    "interactions": [
      {
        "seq": 0,
        "input": "帮我列出当前目录下的文件",
        "output": "",
        "llm_calls": []
      }
    ]
  }
}
```

## 查询 SQLite 存储

除 JSON 威胁日志外，事件与检测结果还持久化在 SQLite 数据库中，可用 `sqlite3` 命令行或任意 SQLite 客户端查询。

### 存储布局

```text
<storage>/
├── ssas_core.db                      # 核心库：raw_events / events / alerts 表（告警统一落此库）
├── modules/<模块名>/
│   ├── process.db                    # 过程库：建模数据、中间结果
│   ├── result.db                     # 结果库：该模块的威胁分析报告（含无风险报告）
│   └── config/                       # 检测规则、基线、阈值参数
└── reports/threat_log/*.json         # OCSF 威胁日志
```

每个 `.db` 文件启用 WAL 模式，包含 `events`、`alerts`、`raw_events` 三张表，表结构一致：关联字段（如 `trace_id`、`session_id`）为独立列，完整数据以 JSON 字符串存于 `data` 列。

时间以双列存储：

- `timestamp`：Unix 浮点秒，用于计算与排序；
- `timestamp_text`：本地时区可读格式（`YYYY-MM-DD HH:MM:SS.mmm`），便于直接查看；旧库打开时自动迁移补列并回填历史数据。

有风险的检测报告由 ThreatAnalysisPipeline 统一写入**主库 ssas_core.db 的 alerts 表**（notify 后台任务与 auth 同步路径均覆盖），因此跨模块查询告警应查主库；各模块的 `result.db` 则保存该模块的全部威胁分析报告（含无风险报告）。告警记录的 `alert_id` 格式为 `{event_id}_{module_name}`，并含 `module_name` 字段，可追溯产生告警的检测模块。

### 查询原始事件（ssas_core.db 的 raw_events 表）

```sql
-- 查看最近 10 条原始事件的关键列
SELECT raw_event_id, event_type, event_class, interaction_seq,
       session_id, agent_id, trace_id, timestamp_text
FROM raw_events
ORDER BY id DESC
LIMIT 10;

-- 按 trace_id 关联查询一次完整交互的所有事件
SELECT event_type, timestamp_text, data
FROM raw_events
WHERE trace_id = 'demo-trace'
ORDER BY id ASC;

-- 查看某条事件的完整三层结构（common / payload / metadata）
SELECT data FROM raw_events WHERE raw_event_id = '<事件ID>';
```

命令行示例：

```bash
sqlite3 ~/.jiuwenswarm/ssas/ssas_core.db \
  "SELECT event_type, event_class, timestamp_text FROM raw_events ORDER BY id DESC LIMIT 10;"
```

### 查询告警（ssas_core.db 的 alerts 表）

```sql
-- 查看最近告警(alert_id 格式为 {event_id}_{module_name},含 module_name 字段)
SELECT alert_id, module_name, risk_level, risk_type, trace_id, timestamp_text
FROM alerts
ORDER BY id DESC
LIMIT 20;
```

```bash
sqlite3 ~/.jiuwenswarm/ssas/ssas_core.db \
  "SELECT alert_id, module_name, risk_level FROM alerts ORDER BY id DESC LIMIT 20;"
```

### 查询检测结果（modules/*/result.db）

```sql
-- 查看某检测模块产出的最近报告(含无风险报告)
SELECT event_id, event_type, risk_level, risk_type, timestamp_text
FROM events
ORDER BY id DESC
LIMIT 20;

-- 按会话过滤报告记录
SELECT event_type, timestamp_text, data
FROM events
WHERE session_id = 'demo-session'
ORDER BY id DESC
LIMIT 20;
```

```bash
sqlite3 ~/.jiuwenswarm/ssas/modules/agent_moss/result.db \
  "SELECT event_type, risk_level, timestamp_text FROM events ORDER BY id DESC LIMIT 20;"
```

## 数据保留时间

过期数据按 TTL 自动清理：

- 主库 `ssas_core.db`：`events`、`raw_events` 表按 `event_ttl_days`（默认 30 天），`alerts` 表按 `alert_ttl_days`（默认 90 天）。
- 模块库：`process.db` 按 `event_ttl_days`，`result.db` 按 `alert_ttl_days`。
- 清理在 `AgentSSASBackend.initialize()` 启动时执行一次，此后每累计 100 次事件上报后台触发一次；TTL 设为 `<= 0` 时禁用对应表的清理。

可通过 `SSAS_EVENT_TTL_DAYS` / `SSAS_ALERT_TTL_DAYS`（或 config.yaml）调整，详见[配置参考](../reference/configuration.md)。

## 相关文档

- [事件格式参考](../reference/event-format.md)：raw_event 三层结构说明
- [启用与禁用检测模块](./enable-disable-detection-modules.md)
- [配置参考](../reference/configuration.md)
