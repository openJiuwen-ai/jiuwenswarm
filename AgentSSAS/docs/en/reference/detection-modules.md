# Detection Modules Reference

Detection modules are the execution units of AgentSSASCore threat detection. Each module is declared by a `module.yaml`, contains one data modeling plugin and one threat analysis plugin, and owns an independent storage directory. Modules are scanned and loaded at startup by `DetectionModuleManager` (`src/agent_ssas/core/framework/module_manager/manager.py`).

Related documentation:

- [Event Format Reference](./event-format.md): definitions of the event types modules subscribe to
- [Enable and Disable Detection Modules](../how-to/enable-disable-detection-modules.md): steps to toggle modules on and off
- [Configuration Reference](./configuration.md): the `ssas.modules` section

## Module Loading Mechanism

`DetectionModuleManager.initialize()` scans the `agent_ssas/core/detection_modules/` directory at startup:

- It walks each subdirectory (skipping directories prefixed with `_` and the `common` shared component directory) and reads the `module.yaml` inside.
- It validates the required fields (`name`, a `data_modeling` plugin, a `threat_analysis` plugin) and raises an exception when any is missing.
- The `enabled` field of `module.yaml` can be overridden via `ssas.modules.<module_name>.enabled` in config.yaml; modules with `enabled` false are skipped outright.
- Plugins are dynamically imported and instantiated: the plugin class is first looked up in the framework's preset plugin directories (`agent_ssas.core.framework.data_modeler.plugins` / `agent_ssas.core.framework.threat_analyzer.plugins`); if not found there, it is looked up in the module directory (`agent_ssas.core.detection_modules.<module_name>.`) under a file name derived from the class name converted to snake_case. When instantiating, the plugin-specific configuration (the `config` field) is passed first; if the constructor takes no parameters, instantiation falls back to no-argument construction.
- Each module gets its own storage manager (the `process.db`, `result.db`, and `config/` directory).
- The subscription table is built from `subscribed_events`. A single module load failure is logged and skipped without affecting other modules.

## module.yaml Field Reference

```yaml
name: <module_name>                  # Required; unique identifier; also used as the storage subdirectory name
display_name: "<display name>"       # Optional; defaults to name
enabled: true                        # Optional; default true; can be overridden by config.yaml ssas.modules.<name>.enabled
event_version: "1.0"                 # Optional; event format version; default "1.0"
subscribed_events:                   # Optional; default ["*"]
  - "tool_input"                     #   base event name (notify mode)
  - "permission_interrupt_tool:auth" #   the :auth suffix declares auth mode
analytic_type_id: 1                  # Optional; analytic type ID (OCSF mapping: 1=Rule, 2=Behavior); default 0
auth_timeout_policy: allow           # Optional; default policy when an auth subscriber times out; default "allow"

plugins:                             # Required; must contain both plugin types below
  - type: data_modeling              # data modeling plugin
    name: <plugin_class_name>        #   looked up in the framework preset directory or the module directory
    model_type: <model_type>         #   data model type produced
  - type: threat_analysis            # threat analysis plugin
    name: <plugin_class_name>
    expected_model_type: <model_type> # data model type consumed (must match the modeling plugin output)
    config:                          # Optional; plugin-specific configuration passed to the plugin constructor
      ...: ...
```

Field descriptions:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique module identifier |
| `display_name` | no | Display name; defaults to `name` |
| `enabled` | no | Enable switch; default `true` |
| `event_version` | no | Event format version; default `"1.0"` |
| `subscribed_events` | no | List of subscribed events; default `["*"]` |
| `analytic_type_id` | no | Analytic type ID, used for the `analytic.type` mapping in OCSF reports: 1=Rule, 2=Behavior; default 0 |
| `auth_timeout_policy` | no | Default policy when an auth-mode subscriber times out; default `allow` |
| `plugins` | yes | Plugin list; must contain one `data_modeling` and one `threat_analysis` plugin |

Plugin types:

- `data_modeling` (data modeling plugin): consumes events and produces data models of the declared `model_type` (such as `agent_behavior_model` or `blank`). Declared fields are `name` (the plugin class name) and `model_type` (the model type).
- `threat_analysis` (threat analysis plugin): consumes models of the `expected_model_type` and outputs threat analysis reports. Declared fields are `name`, `expected_model_type`, and the optional `config` (a plugin-specific configuration dictionary injected at instantiation).

## Subscription Mode Syntax

Each entry in `subscribed_events` is an event spec string with the syntax `event_name[:mode]`:

| Notation | Mode | Description |
|----------|------|-------------|
| `"tool_input"` | notify (default) | Asynchronous background detection; `report_event` does not wait for the result and does not block the runtime |
| `"tool_input:auth"` | auth | Synchronous detection; `report_event` must wait for the result and participates in runtime decisions |
| `"*"` | always notify | Wildcard subscription to all events (auth mode cannot be declared with `:auth`) |

Aggregate event subscriptions are also supported; the manager expands them into their base events:

| Aggregate event | Expanded into base events | auth applies to |
|-----------------|----------------------------|-----------------|
| `one_toolcall_event` | tool_input, tool_output | tool_output |
| `one_llmcall_event` | llm_input, llm_output, tool_input, tool_output | llm_output |
| `one_interaction_event` | invoke_start, invoke_end, llm_input, llm_output, tool_input, tool_output | invoke_end |

auth mode applies only to the closing event of an aggregate event; all other expanded events are notify.

All 3 built-in modules currently subscribe in notify mode.

## Built-in Modules

### agent_moss

AgentMoss behavior chain and PDG detection module, enabled by default. Complete `module.yaml` (`src/agent_ssas/core/detection_modules/agent_moss/module.yaml`):

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

Behavior notes:

- Subscribes to all 6 lifecycle events (notify mode) and converts them into AgentMoss events through the structure adaptation layer (invoke_start→chat_request, llm_input→model_call, llm_output/invoke_end→model_output, tool_input→tool_call, tool_output→tool_result), preserving trace_id, session_id, all sequence numbers, and tool_call_id.
- `analysis_methods` declares three analysis methods: `rule` (deterministic rules), `behavior_chain` (behavior chain analysis), and `pdg` (program dependency graph data leakage analysis).
- The `policy` section holds the runtime parameters of the AgentMoss analysis engine; the key entries:
  - `default_decision`: the default decision when no risk is found.
  - `ask_threshold` / `block_threshold`: when the risk score reaches these thresholds, an ask (ask the user) or block recommendation is produced, respectively.
  - `enforcement_scope`: the analysis scope subject to enforcement (behavior_chain).
  - The `analyze_*` series: switches for each detection dimension (behavior chain, PDG data leakage, destructive shell, sensitive path access, data exfiltration, privileged account operations, persistence operations, obfuscated execution).
  - `enforce_behavior_chain` / `enforce_pdg_data_leakage`: enforcement switches for the corresponding dimensions.
  - `pdg_max_events`: maximum number of history events for PDG analysis.
  - `pdg_trusted_egress_patterns` / `pdg_egress_tool_patterns`: whitelists of trusted egress patterns and egress tool patterns.
  - `max_history_events`: bounded history cap maintained per session.
- The module runs in notify mode: risk results, recommended actions, and sanitized PDG evidence are written to `modules/agent_moss/result.db` and the threat logs; decisions are reported as recommendations (`advisory_notify`) and do not directly block JiuwenSwarm.
- `analytic_type_id: 2` maps to `analytic.type = "Behavior"` in OCSF reports.

### security_rail_detection

Security rail detection module, enabled by default. Complete `module.yaml` (`src/agent_ssas/core/detection_modules/security_rail_detection/module.yaml`):

```yaml
name: security_rail_detection
display_name: "安全护栏检测模块"
enabled: true
event_version: "1.0"
subscribed_events:
  - "permission_interrupt_tool"    # notify mode (default): asynchronous background detection, does not participate in decision blocking
analytic_type_id: 1

plugins:
  - type: data_modeling
    name: BlankDataModeler
    model_type: blank
  - type: threat_analysis
    name: SecurityRailAnalyzer
    expected_model_type: blank
```

Behavior notes:

- Subscribes to the `permission_interrupt_tool` security detection derived event (notify mode) and consumes the `risk_source`, `risk_type`, `risk_level`, `decision`, and `evidence` fields from the event payload.
- The data modeling plugin is `BlankDataModeler` (blank modeling, produces no model content); the threat analysis plugin is `SecurityRailAnalyzer` (located in the module directory as `security_rail_analyzer.py`), which aggregates the interception decisions of other security rails into a risk assessment and writes it to its own result.db and the threat logs.
- `analytic_type_id: 1` maps to `analytic.type = "Rule"` in OCSF reports.

### test_detection

Test detection module, disabled by default. Complete `module.yaml` (`src/agent_ssas/core/detection_modules/test_detection/module.yaml`):

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

Behavior notes:

- Purpose: verifies the module manager's loading, subscription, and storage mechanisms. It subscribes to the wildcard `*` (all events, notify mode); both plugins are blank implementations (`BlankDataModeler` / `BlankThreatAnalyzer`, located in the framework preset plugin directories) and produce no actual detection results.
- Defaults to `enabled: false`; enable it per [Enable and Disable Detection Modules](../how-to/enable-disable-detection-modules.md) when you need to verify the module mechanics.

## Module Storage Structure

Each successfully loaded module gets an independent storage directory (managed by `ModuleStorageManager`):

```text
<storage>/modules/<module_name>/
├── process.db    # Process data (modeled data, intermediate results)
├── result.db     # Result data (alerts, audits, threat analysis reports)
└── config/       # Configuration data (detection rules, baselines, threshold parameters, updated manually)
```

Process and result data live in separate databases, so they can be cleaned independently under different TTLs (`event_ttl_days` / `alert_ttl_days`). For query instructions, see [View Threat Logs](../how-to/view-threat-logs.md).

## Related Documentation

- [Event Format Reference](./event-format.md)
- [Configuration Reference](./configuration.md)
- [Enable and Disable Detection Modules](../how-to/enable-disable-detection-modules.md)
- [Switch Decision Policy](../how-to/switch-decision-policy.md)
