# Quickstart

> This chapter walks you through installing AgentSSAS and running a minimal loop in in-process mode: creating the analysis engine, reporting events, getting risk assessments, and inspecting the persisted data. The whole process requires no JiuwenSwarm, LLM, or internet access. It is intended for all users who want to get started with AgentSSAS quickly.

## Prerequisites

- Python >= 3.11 (Windows / Linux / macOS supported)
- [uv](https://docs.astral.sh/uv/) installed

## Installation

Enter the AgentSSAS directory of the repository, create a virtual environment, and install in editable mode:

```bash
cd AgentSecurity/AgentSSAS
uv venv
uv pip install -e .
```

> If working from the local source code is not convenient, you can also install from the remote repository:
>
> ```bash
> uv pip install "agent-ssas @ git+https://atomgit.com/yieux1/AgentSecurity.git@develop#subdirectory=AgentSSAS"
> ```

This tutorial only uses in-process mode, which requires no extra dependencies. HTTP service mode additionally requires fastapi / uvicorn; see [HTTP Service Mode](./04-http-mode.md).

## Running the Quickstart Example

From the AgentSSAS repository root, run:

```bash
uv run python examples/inprocess_quickstart/main.py
```

The expected output is as follows (the user directory in the storage path varies by system; on Windows it looks like `C:\Users\<user>\.jiuwenswarm\ssas`):

```text
[demo] 存储路径: /home/<user>/.jiuwenswarm/ssas
[demo] event=invoke_start               risk_level=safe     has_risk=False
[demo] event=llm_input                  risk_level=safe     has_risk=False
[demo] event=tool_input                 risk_level=safe     has_risk=False
[demo] event=tool_output                risk_level=safe     has_risk=False
[demo] event=llm_output                 risk_level=safe     has_risk=False
[demo] event=invoke_end                 risk_level=safe     has_risk=False
[demo] event=permission_interrupt_tool  risk_level=safe     has_risk=False
[demo] SQLite 数据库: /home/<user>/.jiuwenswarm/ssas/ssas_core.db (存在)
[demo] OCSF 威胁日志: /home/<user>/.jiuwenswarm/ssas/reports/threat_log/xxx.json
```

### Understanding the Output

The example builds a complete event stream of 7 events: the first 6 lifecycle events simulate an interaction that includes a tool call (`invoke_start` -> `llm_input` -> `tool_input` -> `tool_output` -> `llm_output` -> `invoke_end`), and the 7th `permission_interrupt_tool` simulates another security Rail rejecting a dangerous tool call.

- The first 6 lifecycle events carry no security semantics by themselves, so each one returns `risk_level=safe`
- `permission_interrupt_tool` is a security-detection-derived event, subscribed to by the `security_rail_detection` module in **notify mode**: `report_event` does not wait for the background detection and returns immediately (`safe`); the detection conclusion is written asynchronously to the threat log and the alert table. That is why the 7th line is still `safe`, yet the threat log file in the last line has already been generated
- The `await asyncio.sleep(0.5)` at the end of the example exists precisely to wait for the background detection of notify mode to finish

### Key Points of the Example Code

`examples/inprocess_quickstart/main.py` demonstrates the minimal usage of AgentSSASCore:

1. `AgentSSASConfig.from_dict(None)`: default configuration; storage follows the JIUWENSWARM_HOME convention (`~/.jiuwenswarm/ssas`), which can be overridden with the environment variable `SSAS_HOME`
2. `AgentSSASBackend(config)` plus `await backend.initialize()`: construct the backend and scan and load the detection modules
3. `await backend.report_event(raw_event)`: the single interface for reporting an event with the three-layer structure (`common` / `payload` / `metadata`); it returns a `RiskAssessment`
4. `await backend.close()`: release the SQLite connection (required on Windows, otherwise the WAL file locks the directory)

For the complete event format and the meaning of each field, see [Event Format Reference](../reference/event-format.md).

## Inspecting the Persisted Data

After the run finishes, inspect the `~/.jiuwenswarm/ssas/` directory:

| Data | Path (relative to `~/.jiuwenswarm/ssas/`) |
| ---- | ------------------------------------------ |
| Core database (events, alerts) | `ssas_core.db` (SQLite, contains the `raw_events` table) |
| OCSF threat logs | `reports/threat_log/*.json` |
| Detection module results | `modules/<module_name>/result.db` |

For example, `modules/agent_moss/result.db` and `modules/security_rail_detection/result.db` are the result databases of the two built-in modules.

The storage root directory is resolved with the priority `SSAS_HOME` > `JIUWENSWARM_DATA_DIR` > `JIUWENSWARM_HOME` > `~/.jiuwenswarm`, and the data finally lands in `<root>/ssas/`. For storage-related configuration, see [Configuration Reference](../reference/configuration.md).

## Next Steps

- [Integrating JiuwenSwarm](./03-integrate-jiuwenswarm.md): have JiuwenSwarm automatically collect agent behavior events at runtime
- [HTTP Service Mode](./04-http-mode.md): deploy the analysis engine as a standalone service shared by multiple agents
- [Writing a Custom Detection Module](./05-custom-detection-module.md): implement your own detection logic
- Complete example walkthrough: [examples/inprocess_quickstart/README.md](../../../examples/inprocess_quickstart/README.md)
