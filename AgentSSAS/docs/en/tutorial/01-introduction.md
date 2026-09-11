# Introduction to AgentSSAS

> This chapter introduces what AgentSSAS is, the security problems it solves, and its core concepts, helping you build an overall picture before getting hands-on. It is intended for all users new to AgentSSAS and requires no prior knowledge.

## What Is AgentSSAS

AgentSSAS (Python package name `agent-ssas`) is a general-purpose Agent Security Situational Awareness System that provides real-time threat detection, event collection, and security decision capabilities. AgentSSAS is not bound to any specific agent framework; it currently integrates with the JiuwenSwarm multi-agent framework.

LLM agents perform tool calls, read and write data, and generate content at runtime. These dynamic behaviors introduce security risks that traditional applications do not have. AgentSSAS focuses on three typical categories of problems:

- **Agent runtime behavior risks**: anomalous multi-step behavior chains, such as escalating operations step by step through multiple rounds of elicitation, executing destructive commands, or implanting persistence mechanisms
- **Data leakage**: sensitive data flowing to the outside via LLM inference or tool calls, such as user privacy information being concatenated into a prompt and then written to a file or sent to an external service
- **Tool abuse**: calling tools that are not authorized, or constructing dangerous parameters (such as a shell command that deletes the file system)

The approach of AgentSSAS: within the agent runtime, collect agent behavior events in the form of a security Rail, feed the events into the detection engine for analysis, produce risk assessment results in a unified format, and persist them as traceable threat logs. The entire process follows the **fail-open** design principle: any exception in AgentSSAS itself will not affect the main flow of the agent framework.

## Relationship with JiuwenSwarm / agent-core

AgentSSAS consists of two subsystems, corresponding to two paths within the package:

| Subsystem | Package Path | Responsibility |
| --------- | ------------ | -------------- |
| **AgentSSASCore** | `agent_ssas.core` | General-purpose threat detection engine: data preprocessing, analysis pipeline, detection module management, storage, presentation |
| **AgentSSASClient** | `agent_ssas.backend_client` | Event collection and reporting client: currently provides the openjiuwen client (`agent_ssas.backend_client.openjiuwen`), whose core class `AgentSSASSecurityRail` inherits the `BaseSecurityRail` base class from agent-core, assembles agent behavior into events, and reports them to AgentSSASCore |

The dependency relationships among the three deserve special attention:

- The `agent-ssas` package itself **does not depend on the openjiuwen (agent-core) package**; installing it only requires pyyaml (HTTP mode additionally requires fastapi / uvicorn / httpx)
- **AgentSSASCore can be used fully standalone**: any Python process can create the analysis engine, report events, and get risk assessments (see [Quickstart](./02-quickstart.md))
- Only the **instrumentation code of AgentSSASClient** requires the jiuwenswarm runtime (openjiuwen's `BaseSecurityRail` and others), because it must be attached as a Rail into the agent execution chain of JiuwenSwarm

In other words: if you only want to use the detection engine to analyze your own event stream, you do not need to install JiuwenSwarm; if you want agent behavior in JiuwenSwarm to be automatically collected and detected, you need to go through the integration process (see [Integrating JiuwenSwarm](./03-integrate-jiuwenswarm.md)).

> **Terminology note**: the complete JiuwenSwarm integration solution — AgentSSASCore plus the openjiuwen client in AgentSSASClient — was also referred to as **JiuwenSSAS** in earlier documents. It is merely a logical concept referring to this combination, not a separate system.

## Core Concepts

| Concept | Description |
| ------- | ----------- |
| **Event** | A structured record of agent runtime behavior. There are 7 in total: 6 lifecycle events (`invoke_start` / `llm_input` / `tool_input` / `tool_output` / `llm_output` / `invoke_end`) plus 1 security-detection-derived event (`permission_interrupt_tool`). Events use a three-layer structure: `common` (14 correlation fields), `payload` (event content), `metadata` (reserved for extension) |
| **Detection module** | An independent unit that subscribes to events and performs threat analysis. Declared by a `module.yaml`, composed of one data modeling plugin and one threat analysis plugin, and owning an independent storage directory |
| **Subscription mode** | `notify`: background asynchronous detection that does not block the main flow (all built-in modules currently use this mode); `auth`: synchronously participates in security decisions, and the reporting side waits for the detection result to return |
| **RiskAssessment** | The unified output format of detection results, with 9 fields: `has_risk`, `risk_level`, `risk_type`, `risk_score`, `confidence`, `detected_threats`, `recommended_actions`, `details`, `evidence` |
| **Decision policy** | The rule table that maps a RiskAssessment to a security decision (`decision_policies.yaml`): `observe_only` (default, allow everything, situational awareness only); `active_protection` (critical is rejected and blocked, high / medium / low raise alerts, safe is allowed) |
| **Run mode** | `inprocess` (in-process library mode, default, zero network latency); `http` (standalone FastAPI service, multiple agents share the same analysis engine) |

The risk level `RiskLevel` has five values, from low to high:

```text
safe < low < medium < high < critical
```

### Built-in Detection Modules

AgentSSAS ships with 3 detection modules:

| Module | Default State | Subscribed Events | Description |
| ------ | ------------- | ----------------- | ----------- |
| `agent_moss` | `enabled: true` | 6 lifecycle events | Built-in AgentMoss analysis engine, providing three analysis methods: rule (deterministic rules), behavior_chain (behavior chains), and pdg (data leakage analysis) |
| `security_rail_detection` | `enabled: true` | `permission_interrupt_tool` | Identifies and reports the interception decisions of other security Rails; reporting and presentation only |
| `test_detection` | `enabled: false` | `*` (all events) | Blank plugins that pass all events through, used to verify that the pipeline works |

You can also write your own detection modules; see [Writing a Custom Detection Module](./05-custom-detection-module.md).

## Capabilities and Boundaries of 0.1.0

**Capabilities available in the current version (v0.1.0):**

- Collection and reporting of the 7 event types with the three-layer structure
- Dual run modes: inprocess (in-process, default) and http (standalone FastAPI service)
- Detection module framework: module.yaml declaration + data modeling plugin + threat analysis plugin, automatically scanned and loaded at startup
- 3 built-in detection modules (see the table above)
- Unified risk assessment output (RiskAssessment)
- SQLite persistent storage and OCSF-format threat logs
- fail-open: SSAS exceptions do not affect the main flow of JiuwenSwarm

**Boundaries of the current version:**

- All built-in detection modules use `notify` mode: detection results are written asynchronously to the threat logs and the alert table, and **do not block** the execution of JiuwenSwarm
- The `auth` subscription mode and the `active_protection` decision policy are reserved mechanisms: the framework supports synchronous detection and risk-based handling, but 0.1.0 does not yet ship detection modules that use them
- Therefore, 0.1.0 is positioned primarily for **situational awareness**: visible, queryable, and traceable, but not yet directly intercepting

For the background of these design trade-offs, see [Architecture Principles](../explanation/architecture.md); for detailed descriptions of the notify and auth subscription modes, see [Detection Modules Reference](../reference/detection-modules.md).

## Next Steps

- [Quickstart](./02-quickstart.md): run a minimal in-process loop within ten minutes
- [Integrating JiuwenSwarm](./03-integrate-jiuwenswarm.md): have agent behavior in JiuwenSwarm automatically collected
- [HTTP Service Mode](./04-http-mode.md): multiple agents share the same analysis engine
- [Event Format Reference](../reference/event-format.md): the complete field definitions of events
