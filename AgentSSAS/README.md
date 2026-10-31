# AgentSSAS Agent Security Situational Awareness System

<p align="center">
  <strong>Real-time threat detection · Event collection · Security decisions — general-purpose security situational awareness for AI agents (JiuwenSwarm integration available)</strong>
</p>

<p align="center">
  <a href="README.zh.md">中文</a>
  ·
  <a href="docs/README.md">Documentation</a>
  ·
  <a href="examples/README.md">Examples</a>
  ·
  <a href="CHANGELOG.md">Changelog</a>
  ·
  <a href="https://atomgit.com/yieux1/AgentSecurity">AtomGit</a>
</p>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/version-0.1.0-blue.svg" alt="Version" />
  <img src="https://img.shields.io/badge/os-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="OS Support" />
</p>

## Positioning

Agents invoke LLMs and external tools while executing tasks, which introduces new runtime risks: sensitive data exfiltration through multi-step tool calls, destructive command execution, tool abuse, and dormant anomalous behavior chains planted by prompt injection. AgentSSAS continuously collects agent runtime events, performs threat detection in real time, and produces risk assessments — making the security posture of your agents **visible, auditable, and actionable**.

- **What it is**: a general-purpose Agent Security Situational Awareness System (Python package `agent-ssas`) providing event collection, threat detection, and security decision support, not bound to any specific agent framework
- **What problem it solves**: runtime threat detection, behavior auditing, and security decision support for agents
- **Relationship with JiuwenSwarm**: integrates as a plugin via the AgentSSASClient subsystem (core class `AgentSSASSecurityRail`, subclassing the `BaseSecurityRail` base class from openJiuwen agent-core); JiuwenSwarm only needs one small patch applied, and agent-core itself is never modified
- **Standalone use**: the AgentSSASCore detection engine does not depend on openjiuwen — any Python agent framework can embed it directly through the `AgentSSASBackend` interface

> **Terminology note**: the complete JiuwenSwarm integration solution — AgentSSASCore plus the openjiuwen client in AgentSSASClient — was also referred to as **JiuwenSSAS** in earlier documents. It is merely a logical concept referring to this combination, not a separate system.

## Features

The system consists of two subsystems:

| Subsystem | Package path | Responsibility |
| --------- | ------------ | -------------- |
| **AgentSSASCore** | `agent_ssas.core` | Generic threat detection engine: data preprocessing, analysis pipeline, detection module management, storage, OCSF presentation |
| **AgentSSASClient** | `agent_ssas.backend_client` | Event collection and reporting client: provides the openjiuwen client (`agent_ssas.backend_client.openjiuwen`), whose core class `AgentSSASSecurityRail` subclasses agent-core `BaseSecurityRail`, collects 7 events (6 lifecycle + 1 security-derived), and reports them to the engine |

Core capabilities:

- **Two runtime modes**: in-process mode (`inprocess`, default, zero network latency) and HTTP service mode (`http`, standalone FastAPI service shared by multiple agents)
- **Pluggable detection modules**: modules register declaratively via `module.yaml` and are organized as a "data modeling plugin + threat analysis plugin" pair; new detection capabilities require zero framework changes
- **3 built-in detection modules**: `agent_moss` (rule + behavior-chain + PDG data-leakage analysis), `security_rail_detection` (derivative analysis of other security rails' findings), `test_detection` (pipeline verification, disabled by default)
- **notify / auth subscription modes**: notify runs detection asynchronously in the background without blocking; auth participates synchronously in security decisions with timeout fallback policies
- **Decision policies**: `observe_only` (default, observation-only, allows everything) and `active_protection` (critical blocked, others alerted), switchable via a single config entry
- **fail-open design**: any SSAS failure (initialization error, malformed event, detection timeout) never blocks the JiuwenSwarm main flow
- **OCSF threat logs**: detection results are persisted as OCSF-formatted JSON logs; events and alerts are stored in SQLite

**Roadmap**: a situational-awareness web UI and more detection modules (continuously extending the plugin architecture) are on the plan; 0.1.x currently provides OCSF threat logs and SQLite persistence.

## Documentation and Resources

| Resource | Description |
| -------- | ----------- |
| [docs/](docs/README.md) | Bilingual documentation hub: tutorials, how-to guides, reference, and explanation (zh / en) |
| [examples/](examples/README.md) | 3 runnable examples: in-process quickstart, HTTP server demo, JiuwenSwarm integration |
| [design/](design/README.md) | Development design documents: overall architecture and subsystem designs, test reports |
| [patches/](patches/) | JiuwenSwarm integration patches: local-dependency / online-dependency, pick one |
| [CHANGELOG.md](CHANGELOG.md) | Release history ([Releases](https://atomgit.com/yieux1/AgentSecurity/releases)) |
| [SECURITY.md](SECURITY.md) | Security policy and vulnerability reporting |

Repository layout:

```
AgentSSAS/
├── README.md / README.zh.md     # Bilingual README
├── LICENSE / CHANGELOG.md / SECURITY.md
├── docs/                          # Bilingual user docs (zh / en)
├── examples/                      # 3 runnable examples
├── design/                        # Development design docs and test reports
├── patches/                       # JiuwenSwarm integration patches (local / online)
├── src/agent_ssas/
│   ├── core/                      # AgentSSASCore subsystem (detection engine)
│   └── backend_client/openjiuwen/ # AgentSSASClient subsystem (event collection)
└── tests/                         # 200+ test cases
```

## Requirements

- Python >= 3.11
- Windows / Linux / macOS
- HTTP service mode requires the optional dependency group (fastapi / uvicorn, see Installation)

## Installation

### Remote (recommended)

```bash
uv pip install "agent-ssas @ git+https://atomgit.com/yieux1/AgentSecurity.git@develop#subdirectory=AgentSSAS"
```

### Local (development)

```bash
cd AgentSecurity/AgentSSAS
uv venv
uv pip install -e .

# HTTP service mode (optional)
uv pip install -e ".[http]"
```

## Quick Start

### 60-second tour (no JiuwenSwarm needed)

```bash
uv run python examples/inprocess_quickstart/main.py
```

Expected output (events reported one by one with risk assessments returned):

```text
[demo] 存储路径: ~/.jiuwenswarm/ssas
[demo] event=invoke_start            risk_level=safe     has_risk=False
[demo] event=llm_input               risk_level=safe     has_risk=False
[demo] event=tool_input              risk_level=safe     has_risk=False
[demo] event=tool_output             risk_level=safe     has_risk=False
[demo] event=llm_output              risk_level=safe     has_risk=False
[demo] event=invoke_end              risk_level=safe     has_risk=False
[demo] event=permission_interrupt_tool  risk_level=safe  has_risk=False
```

After the run, inspect the stored data under `~/.jiuwenswarm/ssas/`: `ssas_core.db` (raw events), `modules/*/result.db` (per-module results), and `reports/threat_log/*.json` (OCSF threat logs).

### Integrating with JiuwenSwarm

1. Apply a patch (choose one):

```bash
cd <jiuwenswarm repo root>
# Local dependency (editable, for developing alongside AgentSSAS sources)
git apply <AgentSecurity repo path>/AgentSSAS/patches/jiuwenswarm_ssas_local.patch
# Or online dependency (remote git dependency from AtomGit, for production use)
git apply <AgentSecurity repo path>/AgentSSAS/patches/jiuwenswarm_ssas_online.patch
```

2. Install dependencies and configure: add an `ssas` section (`enabled: true`) to `~/.jiuwenswarm/config/config.yaml`
3. Start JiuwenSwarm; the log line `AgentSSASSecurityRail create success, mode=inprocess` confirms the integration
4. Interact with your agent, then check the stored data under `~/.jiuwenswarm/ssas/`

For the full walkthrough and troubleshooting, see the [integration tutorial](docs/en/tutorial/03-integrate-jiuwenswarm.md).

### More examples

| Example | Entry | Description |
| ------- | ----- | ----------- |
| In-process quickstart | [examples/inprocess_quickstart/](examples/inprocess_quickstart/) | Minimal loop: report events, inspect assessments and stored data |
| HTTP server demo | [examples/http_server_demo/](examples/http_server_demo/) | Start a standalone server and report events from a client |
| JiuwenSwarm integration | [examples/jiuwenswarm_integration/](examples/jiuwenswarm_integration/) | Full flow: apply patch, configure, start, and verify |

## License and Contributing

This project is open source under the [Apache License 2.0](LICENSE).

Contributions are welcome:

- Report bugs and suggest features: [Issues](https://atomgit.com/yieux1/AgentSecurity/issues)
- Submit code and documentation: [Pull Requests](https://atomgit.com/yieux1/AgentSecurity/pulls)
- For security vulnerabilities, follow the private reporting process in [SECURITY.md](SECURITY.md) instead of opening a public issue
