# View Threat Logs

AgentSSASCore's presentation layer writes each threat analysis report to disk as a JSON file in the OCSF (Open Cybersecurity Schema Framework) Detection Finding format. This guide explains where the logs live, the naming convention, what the fields mean, and how to query raw events and detection results from the SQLite storage.

## Log Location

Threat logs are stored under the storage directory:

```text
<storage>/reports/threat_log/*.json
```

Here `<storage>` is `AgentSSASConfig.storage_path` (that is, `<ssas_home>/ssas`). By default this is:

```text
~/.jiuwenswarm/ssas/reports/threat_log/*.json
```

The storage root is resolved with the precedence `SSAS_HOME` > `JIUWENSWARM_DATA_DIR` > `JIUWENSWARM_HOME` > `~/.jiuwenswarm`; see the [Configuration Reference](../reference/configuration.md).

## File Naming Convention

File names follow this format:

```text
threat_{trace_id}_{module_name}_{YYYYMMDD_HHMMSS_mmm}.json
```

- `trace_id`: the trace ID of the event that produced the report; `notrace` when missing.
- `module_name`: the name of the detection module that produced the report (such as `agent_moss` or `security_rail_detection`); `unknown` when missing.
- `YYYYMMDD_HHMMSS_mmm`: the local-timezone human-readable time of the write; the millisecond part prevents multiple reports within the same second from overwriting each other.

Examples:

```text
threat_demo-trace_agent_moss_20260904_165015_475.json
threat_demo-trace_security_rail_detection_20260904_165015_489.json
```

File content is UTF-8 encoded JSON with 2-space indentation; Chinese text is preserved as-is (`ensure_ascii=false`).

## OCSF Field Reference

Reports are based on the OCSF 1.8.0+ Detection Finding event class (`class_uid=2004`) and declare the `ai_operation` profile. The top-level fields fall into three groups: fixed framework fields (built automatically from event data), fixed-format detection module fields (mapped into `finding_info`), and detection module custom fields (carried by the `evidences` array).

### Fixed Top-Level Fields

| Field | Type | Description |
|-------|------|-------------|
| `activity_id` / `activity_name` | int / str | Fixed: `1` / `Create` |
| `category_uid` / `category_name` | int / str | Fixed: `2` / `Findings` |
| `class_uid` / `class_name` | int / str | Fixed: `2004` / `Detection Finding` |
| `severity_id` | int | Risk level mapping: safe=1 (Info), low=2, medium=3, high=4, critical=5 |
| `status` / `status_id` | str / int | Fixed: `New` / `1` (a newly created detection finding) |
| `time` | int | Event timestamp (milliseconds) |
| `trace_id` | str | Trace ID |
| `actor` | object | Agent subject: `name` is the agent_id, `type_id=4`, `type="Application"` |
| `metadata` | object | Product metadata, see below |
| `finding_info` | object | Detection finding details, see below |
| `evidences` | array | Evidence artifacts (OCSF standard Evidence Artifacts), see below |
| `ai_agent` | object | Agent information (ai_operation profile), see below |
| `message_context` | object | Model input/output context (ai_operation profile), see below |
| `ai_operation` | object | Interaction hierarchy (ai_operation profile), see below |

### metadata

```json
{
  "product": {
    "name": "AgentSSAS",
    "vendor_name": "AgentSSAS",
    "feature": { "name": "<detection module name>" }
  },
  "version": "1.0",
  "profiles": ["ai_operation"]
}
```

`metadata.product.feature.name` carries the second-level finder (the detection module name).

### finding_info

| Field | Type | Description |
|-------|------|-------------|
| `uid` | str | Unique identifier of the detection finding (UUID) |
| `desc` | str | Report description produced by the detection module |
| `created_time` | int | Report creation time (millisecond timestamp) |
| `created_time_dt` | str | Report creation time (ISO 8601, UTC) |
| `confidence_id` | int | Confidence band: 0=Unknown, 1=Low, 2=Medium, 3=High |
| `confidence` | str | Name of the confidence band, corresponding to `confidence_id` |
| `confidence_score` | int | Confidence score (0-100), converted from `confidence` (0-1) x 100 (`risk_score` no longer occupies this field) |
| `types` | list[str] | List of risk types, taken from `RiskAssessment.risk_type` |
| `analytic` | object | Detection strategy information: `type_id` comes from `analytic_type_id` in module.yaml (1=Rule, 2=Behavior), `name` is the strategy name produced by the detection module |

### evidences

`evidences` is an array whose entries are evidence artifacts. The framework merges `detected_threats`, `recommended_actions`, and the detection module's custom `evidence` into the `data` field:

```json
[
  {
    "uid": "<UUID>",
    "name": "detection_evidence",
    "data": {
      "detected_threats": [],
      "recommended_actions": ["log"],
      "...": "detection-module-specific evidence fields (such as decision, findings, correlation)"
    }
  }
]
```

Note: the serialized JSON length of `data` is capped at 65536 characters; when exceeded, an `_truncated: true` marker is added.

### ai_agent and message_context

- `ai_agent`: `uid`/`name` are the agent_id, `instance_uid` is the session_id, `type_id=1`, `type="Native"`.
- `message_context`: `ai_role_id`/`ai_role` are mapped by event type (invoke_start/invoke_end → User, llm_input/llm_output → Assistant, tool-related events → Tool); `prompt_text` and `response_text` hold the model input and output respectively (tool inputs and outputs are not included here; they live in `ai_operation`); `application.name` is fixed to `JiuwenSwarm`.

### ai_operation

Organizes event content into an interaction → llm_call → tool_call hierarchy:

```json
{
  "interactions": [
    {
      "seq": 0,
      "input": "<query from invoke_start>",
      "output": "<result from invoke_end>",
      "llm_calls": [
        {
          "seq": 0,
          "input": "<prompt from llm_input>",
          "output": "<response from llm_output>",
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

The current event is filled into the level matching its type and sequence number; upper levels are kept as empty placeholders to preserve the hierarchy; absent sequence fields (`-1`) are likewise preserved as-is.

## Complete Example

The following is an actual threat log (`agent_moss` module, invoke_start event, no risk detected):

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

## Querying the SQLite Storage

Besides the JSON threat logs, events and detection results are also persisted in SQLite databases, which can be queried with the `sqlite3` command line or any SQLite client.

### Storage Layout

```text
<storage>/
├── ssas_core.db                      # Core database: raw_events / events / alerts tables (alerts land here)
├── modules/<module_name>/
│   ├── process.db                    # Process database: modeled data, intermediate results
│   ├── result.db                     # Result database: the module's threat analysis reports (including no-risk ones)
│   └── config/                       # Detection rules, baselines, threshold parameters
└── reports/threat_log/*.json         # OCSF threat logs
```

Each `.db` file enables WAL mode and contains the three tables `events`, `alerts`, and `raw_events` with identical schemas: correlation fields (such as `trace_id` and `session_id`) are dedicated columns, while the full data is stored as a JSON string in the `data` column.

Time is stored in two columns:

- `timestamp`: Unix floating-point seconds, used for computation and sorting;
- `timestamp_text`: local-timezone human-readable format (`YYYY-MM-DD HH:MM:SS.mmm`), convenient for direct inspection; when an older database is opened, the column is added automatically by migration and historical rows are backfilled.

Risky detection reports are written by the ThreatAnalysisPipeline into the **alerts table of the core ssas_core.db** (covering both the notify background task and the auth synchronous path), so cross-module alert queries should target the core database; each module's `result.db` holds all of that module's threat analysis reports (including no-risk ones). An alert record's `alert_id` has the format `{event_id}_{module_name}` and carries a `module_name` field, so the detection module that raised the alert can be traced.

### Querying Raw Events (the raw_events Table in ssas_core.db)

```sql
-- View the key columns of the 10 most recent raw events
SELECT raw_event_id, event_type, event_class, interaction_seq,
       session_id, agent_id, trace_id, timestamp_text
FROM raw_events
ORDER BY id DESC
LIMIT 10;

-- Correlate all events of one complete interaction by trace_id
SELECT event_type, timestamp_text, data
FROM raw_events
WHERE trace_id = 'demo-trace'
ORDER BY id ASC;

-- View the full three-layer structure (common / payload / metadata) of one event
SELECT data FROM raw_events WHERE raw_event_id = '<event_id>';
```

Command line example:

```bash
sqlite3 ~/.jiuwenswarm/ssas/ssas_core.db \
  "SELECT event_type, event_class, timestamp_text FROM raw_events ORDER BY id DESC LIMIT 10;"
```

### Querying Alerts (the alerts Table in ssas_core.db)

```sql
-- View the most recent alerts (alert_id has the format {event_id}_{module_name} and a module_name field)
SELECT alert_id, module_name, risk_level, risk_type, trace_id, timestamp_text
FROM alerts
ORDER BY id DESC
LIMIT 20;
```

```bash
sqlite3 ~/.jiuwenswarm/ssas/ssas_core.db \
  "SELECT alert_id, module_name, risk_level FROM alerts ORDER BY id DESC LIMIT 20;"
```

### Querying Detection Results (modules/*/result.db)

```sql
-- View a detection module's most recent reports (including no-risk ones)
SELECT event_id, event_type, risk_level, risk_type, timestamp_text
FROM events
ORDER BY id DESC
LIMIT 20;

-- Filter report records by session
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

## Data Retention

Expired data is cleaned up automatically by TTL:

- Core database `ssas_core.db`: the `events` and `raw_events` tables per `event_ttl_days` (default 30 days), the `alerts` table per `alert_ttl_days` (default 90 days).
- Module databases: `process.db` per `event_ttl_days`, `result.db` per `alert_ttl_days`.
- The cleanup runs once at `AgentSSASBackend.initialize()` startup and is then triggered in the background once every cumulative 100 event reports; a TTL of `<= 0` disables the cleanup of the corresponding tables.

Adjustable via `SSAS_EVENT_TTL_DAYS` / `SSAS_ALERT_TTL_DAYS` (or config.yaml); see the [Configuration Reference](../reference/configuration.md).

## Related Documentation

- [Event Format Reference](../reference/event-format.md): the three-layer raw_event structure
- [Enable and Disable Detection Modules](./enable-disable-detection-modules.md)
- [Configuration Reference](../reference/configuration.md)
