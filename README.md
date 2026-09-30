<p align="center">
  <img src="docs/assets/images/logo.svg" alt="JiuwenSwarm Logo" width="160" />
</p>

<h1 align="center">JiuwenSwarm</h1>

<p align="center">
  <strong>Understands Your Intent, Evolves Autonomously — Swarm Collaboration for Complex Tasks</strong>
</p>
<p align="center">
  <a href="README.md">English</a>
  ·
  <a href="README_CN.md">Chinese</a>
  ·
  <a href="docs/README_EN.md">Docs (EN)</a>
  ·
  <a href="docs/README.md">Docs</a>
  ·
  <a href="https://openjiuwen.com/en/">Website</a>
  ·
  <a href="https://swarmskills.openjiuwen.com/">Swarm Skills Hub</a>
  ·
  <a href="https://gitcode.com/openJiuwen/jiuwenswarm">GitCode</a>
</p>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="License" />
  </a>
  <a href="https://github.com/openJiuwen-ai/jiuwenswarm/releases">
    <img src="https://img.shields.io/pypi/v/jiuwenswarm.svg" alt="Release" />
  </a>
  <img src="https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/os-Windows%20%7C%20macOS%20%7C%20Linux%20%7C%20HarmonyOS-lightgrey.svg" alt="OS Support" />
</p>

[JiuwenSwarm_Introduction.mp4](docs/assets/videos/JiuwenSwarm_Introduction.mp4)

**JiuwenSwarm** is an Agent system that makes multi-agent collaboration truly work. Designed for developers and teams who need to automate complex tasks, it helps users drive multi-agent collaboration, Skill self-evolution, and tool invocation through natural language — delivering end-to-end from intent to result. It runs on a single machine or across a cluster, and you can reach it from a browser, a terminal, or the chat apps you already use.

### Why JiuwenSwarm

| Capability                      | Value                                                                                                                                                     |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Multi-Agent Collaboration       | The Leader decomposes a complex task and assembles teams, multi agents specialize and negotiate dynamically.                                              |
| Distributed Agent Swarm         | Leader and Teammates deploy across processes and machines, coordinating at scale.                                                                         |
| Swarmflow                       | Deterministic multi-stage workflows via Python scripts: the Leader hands off between stage agents; supports **HITL** (`human` / `human_session`), **team token budget**, and TUI **`/swarmflows`** run-tree monitoring. |
| Skill Self-Evolution            | Automatically detects error signals and user dissatisfaction, then optimizes Skill definitions                                                            |
| Skill Hub Sharing               | Capability assets are built once and reused everywhere, search, install, remix, and publish Skills through the Swarm Skills Hub                           |
| Auto Harness                    | Evaluation drive end-to-end optimization of the Harness itself, which learns and improves in practice with no model-weight training                       |
| AI Infrastructure Compatibility | Compatible with Huawei Cloud MaaS and other mainstream platforms, OpenAI-compatible APIs, and local models                                                |
| Tool Permissions & Security     | Every step is under your control, tools require approval before execution, file access goes through a whitelist, and sensitive operations are intercepted |

## Install

### Desktop

One-click install, no environment setup — the quickest way to try JiuwenSwarm.

| Platform  | Download                                                          | Notes                                         |
| --------- | ----------------------------------------------------------------- | --------------------------------------------- |
| Windows   | [Download Windows Version](https://openjiuwen.com/en/jiuwenswarm) | For Windows 10 / 11                           |
| macOS     | [Download macOS Version](https://openjiuwen.com/en/jiuwenswarm)   | For Intel / Apple Silicon                     |
| HarmonyOS | [Try now](https://openjiuwen.com/en/jiuwenswarm)                  | HarmonyOS PC, installed via the official site |

Download and follow the installer prompts to get started.

On Linux, install via [Command Line](#pip) or [from source](#from-source) below. 

### Command Line

```bash
# Install JiuwenSwarm
pip install jiuwenswarm

# Use China mirror (recommended)
pip install jiuwenswarm -i https://pypi.tuna.tsinghua.edu.cn/simple

# Initialize JiuwenSwarm (first-time setup)
jiuwenswarm-init

# Start JiuwenSwarm
jiuwenswarm-start
```

After launching, visit http://localhost:5173 to open the frontend.

To use TUI (terminal interface), open a new terminal after starting JiuwenSwarm:

```bash
# Install JiuwenSwarm-tui
pip install jiuwenswarm-tui

# Use China mirror (recommended)
pip install jiuwenswarm-tui -i https://pypi.tuna.tsinghua.edu.cn/simple

# Start JiuwenSwarm-tui
jiuwenswarm-tui
```

### From Source

```bash
git clone https://github.com/openJiuwen-ai/jiuwenswarm.git
cd jiuwenswarm
uv venv
uv pip install -e .
```

> For detailed installation instructions, see: [Install Guide](docs/en/InstallGuide.md)

## Quick Start

### Configure Model

JiuwenSwarm supports multiple model platforms: Huawei Cloud MaaS, OpenAI, DeepSeek, DashScope, SiliconFlow, OpenRouter and other OpenAI-compatible APIs, as well as local model deployment.

A default model is the one piece of configuration you cannot skip. Set it in the web UI under **More → Configuration**, or edit `~/.jiuwenswarm/config/config.yaml` directly. The file is created on your first `jiuwenswarm-start`, and saving it reloads the config without a restart.

For example, with DeepSeek：

```yaml
model_name: deepseek-v4-flash
api_base: https://api.deepseek.com
api_key: sk-your-api-key
model_provider: OpenAI
```

### Start a Conversation

The workbench has two spaces, switched from the top-left selector: **Work**, for office, collaboration, and general tasks, and **Code**, for viewing and modifying code in a project directory .

Each conversation runs in one of two execution modes, picked from the selector in the chat input area:

| Mode         | What it does                                                                            | Use it for                                              |
| ------------ | --------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Agent mode   | Single agent handles tasks independently, supports task planning and dynamic adjustment | Most daily tasks, Q&A, code generation, etc.            |
| Cluster mode | Multi-agent collaboration mode, with a Leader orchestrating multiple specialized agents | Large, complex tasks that need multi-role collaboration |

**Cluster Mode** (default)

Example input:

```text
Conduct an in-depth research on the new energy vehicle industry and generate an analysis report.
```

**Agent Mode**

Example input:

```text
Check today's weather in Beijing, and recommend 3 books about artificial intelligence.
```

In IM channels and the TUI, the `/mode` command switches between finer-grained sub-modes (`agent.plan`, `agent.fast`, `code.normal`, `code.team`, `team`). See [Modes](https://github.com/openJiuwen-ai/jiuwenswarm/blob/develop/docs/en/Modes.md).

By default, tools ask for approval before they run. If you would rather not confirm every step, adjust the policy in [Tool Permissions & Security](https://github.com/openJiuwen-ai/jiuwenswarm/blob/develop/docs/en/ToolPermissionsSecurity.md).

> For detailed operation guide, see: [Quick Start](docs/en/Quickstart.md)

## Channels

Enable a channel from the **Web UI**, or in `config.yaml` with `enabled: true` and the credentials for that platform, and you can talk to the same agent from an app you already have open.

| Region        | Channels                                                                                                                                                                                                                                                   |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| China         | [Xiaoyi](docs/en/ChinaChannels.md#xiaoyi), [Feishu](docs/en/ChinaChannels.md#feishu-lark), [DingTalk](docs/en/ChinaChannels.md#dingtalk), [WeCom](docs/en/ChinaChannels.md#wecom-wechat-work), [Personal WeChat](docs/en/ChinaChannels.md#personal-wechat) |
| International | [Telegram](docs/en/InternationalChannels.md#telegram), [Discord](docs/en/InternationalChannels.md#discord), [Slack](docs/en/InternationalChannels.md#slack), [WhatsApp](docs/en/InternationalChannels.md#whatsapp)                                         |

Capabilities differ per platform. Xiaoyi and WhatsApp are private chat only; Feishu, WeCom, DingTalk, Telegram, Discord, and Slack also work in groups, usually by @mentioning the bot. Feishu and WeCom additionally support the Digital Avatar feature. See [Channels](docs/en/Channels.md).



## Documentation

Full index: [Documentation](docs/README_EN.md)

- Install and first run: [Install Guide](docs/en/InstallGuide.md) · [Quick Start](docs/en/Quickstart.md)
- The Web UI layout: [Page Overview](docs/en/Page-Overview.md)
- Configure providers: [Configuration](docs/en/Configuration.md)
- Build and evolve capabilities: [Skills](docs/en/Skills.md) · [Skill Self-Evolution](docs/en/SkillSelfEvolution.md)
- Multi-agent and cluster: [Agent Team](docs/en/AgentTeam.md) · [Distributed Team](docs/en/DistributedTeam.md)
- Memory: [Memory](docs/en/Memory.md) · [Task Memory](docs/en/TaskMemory.md) · [Coding Memory](docs/en/CodingMemory.md)
- Automation: [Scheduled Tasks](docs/en/ScheduledTasks.md)
- Terminal: [Quick Start (TUI)](docs/en/Quickstart_tui.md) · [Slash Commands](docs/en/SlashCommands.md) · [SwarmFlow (TUI)](docs/en/TUISwarmFlowGuide.md)
- Extend and integrate: [MCP Configuration](docs/en/MCPConfiguration.md) · [A2A](docs/en/A2A.md) · [E2A Protocol](docs/en/E2A-protocol.md)

## Latest Updates

- **2026-08-06** — `v0.2.4.beta3`  Focuses on cutting cold-start latency in the Agent instance launch path.
- **2026-07-28** — `v0.2.4.beta2` Improves the scheduling mechanism for tasks and refines front-end interaction logic.
- **2026-07-24** — `v0.2.4.beta1` Builds out the Code work-mode system and its front-end support: workspace selection and switching, and code-diff display.
- **2026-07-14** — `v0.2.3` The collaboration capabilities in cluster mode have been enhanced, with new features such as browser sub-agent isolation and support for online sessions within the same session; image attachments and multimodal conversations; Skill-Omni turns visual knowledge into reusable multimodal Skills, with a usage-experience loop improving skill dispatch; new TUI commands (/keybindings, /simplify, /review, /security-review, /btw); stability fixes..

Full notes for every version are on [GitHub Releases](https://github.com/openJiuwen-ai/jiuwenswarm/releases).

## FAQ

For solutions to common issues, see: [FAQ](docs/en/FAQ.md).

## Contributing

We welcome developers to contribute to JiuwenSwarm. You can contribute in the following ways:

- Report bugs, feature requests, or usage issues: [Issues](https://github.com/openJiuwen-ai/jiuwenswarm/issues)
- Submit code, documentation, or examples: [Pull Requests](https://github.com/openJiuwen-ai/jiuwenswarm/pulls)
- Share Skills: [Swarm Skills Hub](https://swarmskills.openjiuwen.com/)

Read the [Contributing Guide](docs/en/Contributing.md) first for the debugging workflow, code style, and commit conventions. The contribution map is on the [openJiuwen contribution page](https://openjiuwen.com/en/contribute).

### Contributors

Thanks to all developers who have contributed to JiuwenSwarm: [View Contributor List](https://github.com/openJiuwen-ai/jiuwenswarm/graphs/contributors)

## Community

| Channel          | Purpose                                                      | Link                                                          |
| ---------------- | ------------------------------------------------------------ | ------------------------------------------------------------- |
| Website          | Product info, updates, and ecosystem                         | [Visit Website](https://openjiuwen.com/en/)                   |
| SIG              | Technical roadmap, engineering practices, ecosystem building | [Join SIG](https://openjiuwen.com/en/community/sig-center)    |
| Swarm Skills Hub | Browse, publish, and reuse JiuwenSwarm Skills                | [Visit Swarm Skills Hub](https://swarmskills.openjiuwen.com/) |

## License

This project is licensed under [Apache License 2.0](LICENSE).

This product serves solely as a workflow orchestration tool and does not embed any AI model capabilities. When users integrate AI models for specific business scenarios, they shall bear full responsibility for compliance obligations under the EU AI Act and other relevant regulatory frameworks.
