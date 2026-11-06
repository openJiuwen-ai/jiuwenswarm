# Writing a Custom Detection Module

> This chapter explains how to write a custom detection module for AgentSSAS: creating the directory, writing module.yaml, implementing the two kinds of plugins (data modeling and threat analysis), and loading and verification. It is intended for users who need to extend AgentSSAS with their own detection logic beyond the built-in modules. Reading [Quickstart](./02-quickstart.md) first is recommended.

## Anatomy of a Detection Module

A detection module = **1 module.yaml declaration + 1 data modeling plugin + 1 threat analysis plugin**, placed as an independent directory under:

```text
src/agent_ssas/core/detection_modules/<module_name>/
├── __init__.py          # Package marker
├── module.yaml          # Module declaration (required)
└── <plugin_file>.py     # Module-specific plugins (optional; framework preset plugins can also be reused)
```

The two kinds of plugins each have their own role and are chained together through the pipeline:

- **Data modeling plugin (DataModeler)**: takes an event description json (`event_desc`) as input and outputs modeled data in a specific format
- **Threat analysis plugin (ThreatAnalyzer)**: consumes the modeled data and outputs a threat analysis report (with fields such as `has_risk` and `risk_level`)

`test_detection` under `agent_ssas.core.detection_modules` is the simplest reference: it directly reuses the blank plugins preset by the framework, passes all events through for statistics and reporting, and is used to verify that the pipeline works. This tutorial also uses it as the starting point.

## module.yaml Field Reference

Take the declaration of `test_detection` as an example:

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

The meaning of each field:

| Field | Required | Default | Description |
| ----- | -------- | ------- | ----------- |
| `name` | Yes | — | Unique module identifier; also determines the name of the storage directory `modules/<name>/` |
| `display_name` | No | Same as `name` | Display name |
| `enabled` | No | `true` | Whether the module is enabled; when `false`, it is skipped during loading |
| `event_version` | No | `"1.0"` | Event format version |
| `subscribed_events` | No | `["*"]` | List of subscribed event specifiers; see [below](#specifying-subscribed-events) for the syntax |
| `analytic_type_id` | No | `0` | Analysis type identifier |
| `plugins` | Yes | — | Plugin declaration list; must contain exactly one `type: data_modeling` entry and one `type: threat_analysis` entry |

Fields of each entry in the `plugins` list:

| Field | Description |
| ----- | ----------- |
| `type` | `data_modeling` (data modeling) or `threat_analysis` (threat analysis) |
| `name` | Plugin class name. At load time, the framework first searches the preset plugin directories, then the module directory |
| `model_type` | Data type identifier of the modeling plugin (data_modeling only) |
| `expected_model_type` | The data type the analysis plugin expects to consume (threat_analysis only); must match the `model_type` of the paired modeling plugin |
| `config` | Optional; a plugin-specific configuration dict passed to the plugin constructor |

## Step-by-Step Tutorial

Now create a sample module `my_detection`: it subscribes to all events and reports high risk for any event whose content contains `rm -rf`.

### Step 1: Create the Module Directory

```bash
cd AgentSecurity/AgentSSAS
mkdir src/agent_ssas/core/detection_modules/my_detection
touch src/agent_ssas/core/detection_modules/my_detection/__init__.py
```

> The module manager scans the `detection_modules/` directory at startup, skips directories starting with `_` and `common` (the shared components directory), and loads only the subdirectories that contain `module.yaml`.

### Step 2: Write module.yaml

Create `src/agent_ssas/core/detection_modules/my_detection/module.yaml`:

```yaml
name: my_detection
display_name: "Sample Detection Module"
enabled: true
event_version: "1.0"
subscribed_events: ["*"]
analytic_type_id: 1

plugins:
  - type: data_modeling
    name: MyDataModeler
    model_type: my_model
  - type: threat_analysis
    name: MyAnalyzer
    expected_model_type: my_model
```

**Get it running before customizing**: if you have not written the plugins yet, you can temporarily change the two `name` values to `BlankDataModeler` / `BlankThreatAnalyzer` (blank plugins preset by the framework) to first verify that the module is scanned and loaded, then replace them with your own plugins.

### Step 3: Implement the Plugins

Plugins are plain Python classes that implement a specific protocol; there is no base class to inherit from:

**Data modeling plugin interface**:

- Class attribute `name`: the plugin class name, matching the `name` in module.yaml
- Class attribute `model_type`: the identifier of the modeled data type
- Method `async def build_model(self, event_desc: dict) -> Any`: takes the event description json as input and returns the modeled data

**Threat analysis plugin interface**:

- Class attribute `name`: the plugin class name
- Class attribute `expected_model_type`: the expected modeled data type
- Method `async def analyze(self, model_data: Any) -> dict`: takes the modeled data as input and returns a threat analysis report

The `event_desc` received by `build_model` is the json of the engine's unified event after serialization; the main structure:

```text
event_desc
├── event_version     # Event format version
├── event_node        # Event node; fields listed below
├── aux_ids           # Correlation IDs (interaction_seq / session_id / agent_id / trace_id /
│                     #   llm_call_seq / tool_call_seq / tool_call_id)
├── trace             # Tracing structure
└── event_id          # Unique event identifier (UUID)
```

Commonly used fields in `event_node`:

| Field | Description |
| ----- | ----------- |
| `event_type` | Event type (for example `tool_input`) |
| `event_class` | Event category: `lifecycle` / `security` |
| `node_type` | Node type: `session` / `interaction` / `llm_call` / `tool_call` |
| `action_name` | Action name. For tool events it is the tool name (for example `bash`); for LLM events it is `llm_call` |
| `input_content` | Input content. For `tool_input` it is the tool arguments; for `llm_input` it is the prompt |
| `output_content` | Output content. For `tool_output` it is the tool result; for `llm_output` it is the response |
| `risk_source` / `risk_type` / `risk_level` / `risk_assessment` | Risk fields carried by security detection events (`event_class="security"`) |

Create `src/agent_ssas/core/detection_modules/my_detection/my_data_modeler.py`:

```python
from __future__ import annotations

from typing import Any


class MyDataModeler:
    """Sample data modeling plugin: extracts a compact detection view from the event node."""

    name: str = "MyDataModeler"
    model_type: str = "my_model"

    async def build_model(self, event_desc: dict[str, Any]) -> dict[str, Any]:
        event_node = event_desc.get("event_node", {})
        return {
            "event_type": event_node.get("event_type", ""),
            "event_class": event_node.get("event_class", ""),
            "action_name": event_node.get("action_name", ""),
            "input_content": event_node.get("input_content", ""),
        }
```

Create `src/agent_ssas/core/detection_modules/my_detection/my_analyzer.py`:

```python
from __future__ import annotations

from typing import Any


class MyAnalyzer:
    """Sample threat analysis plugin: detects events whose content contains rm -rf."""

    name: str = "MyAnalyzer"
    expected_model_type: str = "my_model"

    async def analyze(self, model_data: Any) -> dict[str, Any]:
        if not isinstance(model_data, dict):
            return self._empty_report()

        action_name = model_data.get("action_name", "")
        input_content = str(model_data.get("input_content", ""))
        if "rm -rf" in input_content:
            return {
                "has_risk": True,
                "risk_level": "high",
                "risk_type": "destructive_command",
                "risk_score": 75.0,
                "confidence": 1.0,
                "detected_threats": ["destructive_command"],
                "evidence": {
                    "action_name": action_name,
                    "input_content": input_content,
                },
                "module_name": "my_detection",
            }
        return self._empty_report()

    @staticmethod
    def _empty_report() -> dict[str, Any]:
        return {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "",
            "risk_score": 0.0,
            "confidence": 1.0,
            "detected_threats": [],
            "evidence": {},
            "module_name": "my_detection",
        }
```

**Plugin file naming rule**: when the module manager searches for a plugin in the module directory, it derives the file name by converting the class name to snake_case. `MyDataModeler` corresponds to `my_data_modeler.py`, and `MyAnalyzer` corresponds to `my_analyzer.py`. A mismatched file name leads to an `ImportError`. If the plugin class already exists in the framework preset plugin directories (`agent_ssas.core.framework.data_modeler.plugins` / `agent_ssas.core.framework.threat_analyzer.plugins`), such as `BlankDataModeler`, no file needs to be provided in the module directory.

### Step 4: Restart to Load

Detection modules are scanned and loaded when the backend initializes:

- Standalone run (for example, the [Quickstart](./02-quickstart.md) example): just run the script again
- JiuwenSwarm integration (inprocess mode): restart `jiuwenswarm-start`
- HTTP service mode: restart the server

When loading succeeds, the log contains:

```text
检测模块加载完成: name=my_detection, events=['*']
```

A single module failing to load only logs the exception and skips that module, without affecting the other modules or the main flow (fail-open).

## Specifying Subscribed Events

The `subscribed_events` list supports the following syntax:

| Specifier | Meaning |
| --------- | ------- |
| `"*"` | Wildcard; subscribes to all events (always notify mode) |
| `"tool_input"` | A concrete event name, such as the 6 lifecycle events or `permission_interrupt_tool` |

Advanced syntax (reserved mechanism):

- `"tool_input:auth"`: the `:auth` suffix means **auth mode**, synchronous detection where the reporting side waits for the detection result to return; without the suffix, the default is **notify mode** (background asynchronous detection, non-blocking). All built-in modules currently use notify mode
- Aggregate events: `one_toolcall_event` / `one_llmcall_event` / `one_interaction_event` expand into subscriptions of the related base events according to the engine's internal rules, with auth mode applied to the closing event

For the complete description of subscription modes and decision policies, see [Detection Modules Reference](../reference/detection-modules.md) and [Switch Decision Policy](../how-to/switch-decision-policy.md).

## Passing Module-Specific Configuration

Plugin-specific configuration is passed through `plugins[].config` in module.yaml. At load time, the plugin constructor is called with this dict as the argument first; if the constructor accepts no arguments, instantiation falls back to no-argument construction. The built-in module `agent_moss` uses exactly this pattern:

```yaml
plugins:
  - type: threat_analysis
    name: AgentMossAnalyzer
    expected_model_type: agent_behavior_model
    config:
      analysis_methods: [rule, behavior_chain, pdg]
      risk_threshold: low
      max_history_events: 200
```

Correspondingly, a plugin can receive and keep the configuration in its constructor:

```python
class MyAnalyzer:
    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.keyword = cfg.get("keyword", "rm -rf")
```

On the jiuwenswarm side, the `ssas.modules.<module_name>` section of `~/.jiuwenswarm/config/config.yaml` can also override module behavior; currently the `enabled` switch is supported (for example, temporarily enabling `test_detection`):

```yaml
ssas:
  modules:
    test_detection:
      enabled: true
```

> The remaining plugin-specific configuration in the `ssas.modules` section is reserved for extension; the current version follows the declarations in module.yaml. For the complete description of configuration items, see [Configuration Reference](../reference/configuration.md).

## Verification

Take the [Quickstart](./02-quickstart.md) example: the 7 events reported by `uv run python examples/inprocess_quickstart/main.py` flow through `my_detection`, and the tool arguments of the `permission_interrupt_tool` event contain `rm -rf /`, which the sample analysis plugin flags as `high`. Verification methods:

1. Check whether `~/.jiuwenswarm/ssas/modules/my_detection/result.db` contains detection result records of this module (the directory is created automatically after the module is first loaded)
2. Check whether OCSF threat logs whose file names contain `my_detection` appear under `~/.jiuwenswarm/ssas/reports/threat_log/`
3. Confirm in the load log that `检测模块加载完成: name=my_detection, events=['*']`

Note: because the current mode is notify, the return value of `report_event` is still the aggregated immediate result (usually `safe`); the high-risk conclusion is written asynchronously to `result.db` and the threat log, without blocking the main flow.

## Related Resources

- [src/agent_ssas/core/detection_modules/test_detection/](../../../src/agent_ssas/core/detection_modules/test_detection/): the simplest module reference
- [src/agent_ssas/core/detection_modules/agent_moss/module.yaml](../../../src/agent_ssas/core/detection_modules/agent_moss/module.yaml): an example of a complex module with full plugin configuration
- [Event Format Reference](../reference/event-format.md): the three-layer event structure and field definitions
- [Detection Modules Reference](../reference/detection-modules.md): built-in modules and the complete module.yaml field descriptions
- [Architecture Principles](../explanation/architecture.md): where detection modules sit in the engine
