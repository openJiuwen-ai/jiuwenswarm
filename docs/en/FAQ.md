# JiuwenSwarm FAQ

> **Version Sync**: This document should be kept in sync with the Chinese version [`docs/zh/FAQ.md`](../zh/FAQ.md). When updating one, please update the other.

---

## Installation & Environment

### Q: On start I see "Python version not supported"

Ensure Python version ≥3.11 and <3.14 (3.11 or 3.12 recommended).

```bash
python --version
```

If the version doesn't match, install a supported version and try again.

### Q: On start I see "Node.js not found"

JiuwenSwarm's frontend depends on Node.js. Install version 18.x or newer.

```bash
node --version
```

Download: [https://nodejs.org](https://nodejs.org)

### Q: pip install is slow or times out

Use a China mirror:

```bash
# Tsinghua mirror (recommended)
pip install jiuwenswarm -i https://pypi.tuna.tsinghua.edu.cn/simple

# Aliyun mirror
pip install jiuwenswarm -i https://mirrors.aliyun.com/pypi/simple/
```

### Q: How do I check the installed version?

```bash
jiuwenswarm --version
```

Or:

```bash
pip show jiuwenswarm
```

### Q: How do I uninstall JiuwenSwarm?

```bash
pip uninstall jiuwenswarm
```

---

## Model Configuration

### Q: Which model providers are supported?

JiuwenSwarm supports multiple model platforms: Huawei Cloud MaaS, OpenAI, DeepSeek, DashScope, SiliconFlow, InferenceAffinity, OpenRouter and other OpenAI-compatible APIs, as well as local model deployment.

### Q: Model configuration test failed — what to check?

Check each item:

- **API Key**: Is it correct and not expired?
- **API Base URL**: Is it accessible? Do not include the `/chat/completions` suffix
- **Model name**: Does it match the provider, e.g. `gpt-4o`, `deepseek-chat`?
- **model_provider**: Is the correct provider type selected?

### Q: How should I fill in api_base?

Enter the API URL provided by the service. **Do not include the `/chat/completions` suffix** — the system appends it automatically.

Examples:

| Provider | api_base |
|----------|----------|
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com` |
| DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` |

### Q: Do I need to restart after saving model configuration?

After clicking Save, the backend automatically restarts to load the new configuration. No manual action needed.

---

## Startup & Runtime

### Q: Cannot access the frontend after starting?

1. Confirm the service is running — the terminal should show:

```
[INFO] API server running at http://localhost:8000
[INFO] Web server running at http://localhost:5173
```

2. Visit `http://localhost:5173` in your browser
3. If the port is occupied, specify a custom port:

```bash
jiuwenswarm-web --host 0.0.0.0 --port <custom-port>
```

### Q: How to use JiuwenSwarm on a remote server?

Bind to an externally accessible address when starting:

```bash
jiuwenswarm-web --host 0.0.0.0 --port <custom-port>
jiuwenswarm-app
```

Then access via `http://<server-ip>:<port>`.

### Q: How to start TUI mode?

TUI requires a separate installation. Open a new terminal after starting JiuwenSwarm:

```bash
pip install jiuwenswarm-tui
jiuwenswarm-tui
```

---

## Version Upgrades

### Q: How to upgrade JiuwenSwarm?

**Routine upgrade** (e.g. 0.2.0 → 0.2.1):

```bash
pip install --upgrade jiuwenswarm
```

**Major version upgrade** (crossing version 0.1.7):

1. Back up your data:

| Data Type | Path | Description |
|-----------|------|-------------|
| Memory data | `~/.jiuwenswarm/agent/memory` | Conversation memory |
| Custom skills | `~/.jiuwenswarm/agent/skills` | Custom Skills |
| Configuration | `~/.jiuwenswarm/config` | App settings |

2. Upgrade and reinitialize:

```bash
pip install --upgrade jiuwenswarm
jiuwenswarm-init
```

3. Migrate data: copy backed-up data back to the corresponding directories

### Q: Service won't start after upgrade — what to do?

For major version upgrades, re-run `jiuwenswarm-init` to reinitialize the configuration. After initialization, check if the configuration files need updating.

---

## Usage

### Q: How to choose between the three execution modes?

| Mode | Best For |
|------|----------|
| Plan Mode | Complex tasks requiring step-by-step execution with confirmation at each step |
| Performance Mode | Simple tasks, fast response |
| Swarm Mode | Large complex tasks requiring multi-Agent specialization and coordination (default) |

### Q: When should I clear a session?

| Scenario | Description |
|----------|-------------|
| Topic switch | Starting a completely different topic |
| Context confusion | Too much conversation history, model understanding drifts |
| Repetitive/incorrect replies | Model stuck in a loop or giving irrelevant responses |
| Privacy cleanup | Session contains temporary sensitive information |

### Q: How does Skill self-evolution work?

When an execution error occurs or the user expresses dissatisfaction, the system automatically detects the signal and optimizes the Skill definition — making capabilities stronger with use. No manual intervention needed.

---

## More Help

- **Documentation**: [docs/README_EN.md](../README_EN.md)
- **Issue Tracker**: [GitCode Issues](https://gitcode.com/openJiuwen/jiuwenswarm/issues)
- **Community**: Follow openJiuwen community events