# Configuration

JiuwenClaw reads settings from `config/config.yaml`, `.env`, and the web UI. This document explains **what you can change in the UI**, **what must be edited in files**, and what each option does.

---

## 1. Configurable in the web UI

These can be changed in the web app; values are written back to `.env` or config as appropriate.

**Path**: left sidebar → **Configuration**

![Configuration](../assets/images/config.png)

**Saved to**: `.env` (environment variables)

| Field | Environment variable | Description |
|--------|------------------------|-------------|
| `api_base` | `API_BASE` | Model API base URL (e.g. `https://api.deepseek.com`) |
| `api_key` | `API_KEY` | Model API key |
| `model` | `MODEL_NAME` | Model name (e.g. `deepseek-chat`) |
| `model_provider` | `MODEL_PROVIDER` | Provider (e.g. `OpenAI`) |
| `embed_api_base` | `EMBED_API_BASE` | Embedding API base URL |
| `embed_api_key` | `EMBED_API_KEY` | Embedding API key |
| `embed_model` | `EMBED_MODEL` | Embedding model name |
| `video_model` | `VIDEO_MODEL_NAME` | Video processing model |
| `audio_model` | `AUDIO_MODEL_NAME` | Audio processing model |
| `vision_model` | `VISION_MODEL_NAME` | Vision model |
| `jina_api_key` | `JINA_API_KEY` | Jina search API key |
| `serper_api_key` | `SERPER_API_KEY` | Serper search API key |
| `perplexity_api_key` | `PERPLEXITY_API_KEY` | Perplexity API key |
| `github_token` | `GITHUB_TOKEN` | GitHub PAT; SkillNet search/install uses the GitHub API |
| `free_search_proxy_url` | `FREE_SEARCH_PROXY_URL` | Optional HTTP/HTTPS proxy for free search and webpage fetch, for example `http://username:password@proxyhk.huawei.com:8080` |
| `evolution_auto_scan` | `EVOLUTION_AUTO_SCAN` | Auto-scan evolvable skills after each turn (`true`/`false`) |

**Note**: After saving, the backend restarts to load new settings. Model fields (`api_base`, `api_key`, `model`, `model_provider`) are required.

## 2. Not configurable in the web UI

Edit **`config/config.yaml`** or **`.env`** directly; there is no UI for these.

### 2.1 `config.yaml` — file-only fields

| Path | Description |
|------|-------------|
| `react.agent_name` | Agent name, default `main_agent` |
| `react.max_iterations` | Max iterations, default 50 |
| `react.context_engine_config.enable_reload` | Enable context reload |
| `react.evolution.enabled` | Enable online skill evolution |
| `react.evolution.skill_base_dir` | Skill root, default `workspace/agent/skills` |
| `tools` | Enabled tools (e.g. `todo`, `skill`) |
| `browser.remote_debugging_address` | Remote debugging address |
| `browser.remote_debugging_port` | Remote debugging port |
| `browser.user_data_dir` | Chrome user data directory |
| `browser.profile_directory` | Chrome profile directory |

---

### 2.2 Environment-only fields

| Variable | Description |
|----------|-------------|
| `HEARTBEAT_TIMEOUT` | Heartbeat request timeout (seconds) |
| `HEARTBEAT_RELAY_CHANNEL_ID` | Heartbeat relay channel (overrides `target` in config) |
| `HEARTBEAT_INTERVAL` | Heartbeat interval (seconds), overrides `every` in config |
| `BROWSER_RUNTIME_MCP_ENABLED` | Enable browser MCP runtime |
| `BROWSER_RUNTIME_MCP_CLIENT_TYPE` | MCP client type (`stdio` / `sse` / `streamable-http`) |
| `BROWSER_RUNTIME_MCP_SERVER_PATH` | MCP server URL |
| `PLAYWRIGHT_CDP_URL` | Playwright CDP URL for Chrome |
| `PLAYWRIGHT_TOOL_TIMEOUT_S` | Playwright tool timeout (seconds) |
| `BROWSER_TIMEOUT_S` | Browser task timeout (seconds) |
| `JIUWENCLAW_CONFIG_DIR` | Custom config directory |
| `JIUWENCLAW_DATA_DIR` | Absolute path to the user data root (`config/`, `agent/`, `.logs`, etc.). If unset, defaults to `~/.jiuwenclaw`. Set in the shell or service environment **before** starting the process so workspace paths resolve from the first import; defining it only in `config/.env` is often too late for that bootstrap. |
| `JIUWENCLAW_DISABLE_CRON_TOOLS` | Set to `1` to disable Agent-side cron tool registration and hide cron-tool prompt text |

See `.env.template` for more variables.

---

### 2.3 Precedence

- **Environment variables** override **`config.yaml`**
- Example: `react.model_name: ${MODEL_NAME:-deepseek-chat}` reads `MODEL_NAME` first, then falls back to `deepseek-chat`.
- Values saved from the Config panel go to `.env` and take effect on next start.
