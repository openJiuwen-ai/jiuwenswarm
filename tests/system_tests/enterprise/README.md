# Enterprise Gateway Runtime 系统测试

本目录包含 **Enterprise 版 Gateway + Runtime Management** 的端到端（E2E）系统测试，验证从 Web Channel 发起到 AgentServer 拉起、LLM 调用、流式回复的完整链路。

主用例：`test_gateway_runtime_e2e.py::test_gateway_runtime_process_deploy_and_chat`

## 测试目标

在本地无 K8s、无真实 LLM 的前提下，模拟企业生产环境的关键路径：

1. Gateway 加载 EE 扩展（`runtime_management_extension`），以 **Process 模式** 拉起 AgentServer
2. Gateway 通过 Runtime Session 与 AgentServer 建立 WebSocket 通信
3. 测试客户端经 **WebChannel**（`ws://127.0.0.1:{web_port}/ws`）发送 `chat.send` 请求
4. AgentServer 调用 Mock LLM，流式返回内容，经 Gateway 回传给客户端
5. Gateway 与 AgentServer **共用同一份 SQLite** 企业配置库

## 链路概览

```
┌─────────────┐     WebSocket      ┌──────────────┐    WS/E2A     ┌───────────────┐
│ E2E 测试客户端 │ ───────────────► │ Gateway       │ ────────────► │ AgentServer    │
│ (websockets)  │   chat.send      │ app_gateway   │  Runtime Mgmt │ (subprocess)   │
└─────────────┘                    └───────┬──────┘               └───────┬───────┘
                                           │                              │
                                           │ ProcessServiceHandler        │ HTTP SSE
                                           │ 拉起子进程                    ▼
                                           │                      ┌───────────────┐
                                           │                      │ Mock LLM       │
                                           └──────────────────────│ (OpenAI 兼容)  │
                                                                  └───────────────┘

共享 SQLite：.runs/<timestamp>/jiuwenswarm.db
```

## 目录结构

```
enterprise/
├── README.md                      # 本文档
├── .env                           # 仅 Gateway 进程使用的环境变量模板
├── conftest.py                    # pytest fixture：创建 .runs/<timestamp>/ 运行目录
├── test_gateway_runtime_e2e.py    # Gateway + AgentServer 运行时 E2E
├── test_logging_config_process_e2e.py  # logging_config 真实进程 E2E（Manager + Gateway + AgentServer）
├── test_logging_config.md  # 上述用例五阶段调用链文档
├── e2e_helpers.py                 # 进程 E2E 公共 helper
├── mock_llm_server.py             # 本地 Mock LLM（OpenAI SSE 流式）
├── agentserver_launcher.py        # AgentServer 启动入口（注入测试 stub 依赖）
└── .runs/                         # 每次运行的产物（已加入 .gitignore）
    └── <timestamp>/
        ├── jiuwenswarm.db         # Gateway / AgentServer 共用 SQLite
        ├── mock_llm.log
        ├── gateway/
        │   ├── gateway.log
        │   └── .jiuwenswarm/       # Gateway HOME / workspace
        └── server/
            ├── agentserver.log
            └── .jiuwenswarm/       # AgentServer HOME / workspace
```

## 用例主要步骤

### 1. 准备运行目录

`conftest.py` 的 `enterprise_run_dirs` fixture 每次测试创建：

- `run_home`：`.runs/<YYYYMMDD_HHMMSS_ffffff>/`
- `gateway_home`：`run_home/gateway/`（Gateway 的 `HOME`）
- `server_home`：`run_home/server/`（AgentServer 的 `HOME`，经 `AGENT_SERVER_HOME` 注入）

### 2. 环境变量隔离

| 组件 | 环境来源 | 说明 |
|------|----------|------|
| Gateway | `enterprise/.env` + `_build_gateway_env()` | 含 Runtime、扩展目录、Mock LLM 地址等 |
| Mock LLM | 最小系统 env | 不加载 `enterprise/.env` |
| AgentServer | `_agent_env_vars()` | 由 Gateway 拉起时注入，**不继承** Gateway 全量 env |

AgentServer 侧 LLM 配置（`API_BASE` / `API_KEY` / `MODEL_*`）来自 Gateway 进程 env，经 `runtime_management_client._agent_env_vars()` 转发；**不会**读取 `enterprise/.env` 或 server workspace 的 `.env`。

### 3. 共享 SQLite

测试运行时设置：

- `GATEWAY_DB_TYPE=sqlite`
- `GATEWAY_SQLITE_PATH=<run_home>/jiuwenswarm.db`（绝对路径）
- `JIUWENCLAW_ID=enterprise_e2e_001`

Gateway 与 AgentServer 读写同一 DB 文件，用于企业配置（`GatewayDb`）。

### 4. 启动进程

按顺序启动：

1. **Mock LLM**：`mock_llm_server.py --port <mock_port>`（5 token × 0.05s 快速流式）
2. **Gateway**：`python -m jiuwenclaw.app_gateway --port <web_port>`
   - 等待日志：`using extension AgentServerClient`、`WebChannel 已启动`
3. 首次 chat 请求触发 Runtime **Process deploy**，subprocess 拉起 AgentServer

### 5. WebChannel 请求

测试作为 Web 客户端连接 `ws://127.0.0.1:{web_port}/ws`：

1. 收到 `connection.ack`
2. 发送 `chat.send`（`session_id=enterprise_sess_001`，`content=say hello`，`mode=agent.fast`）
3. 等待 `accepted` 与流式完成（`chat.delta` / `chat.final` 或 `chat.processing_status is_complete`）

## 主要断言

| 断言项 | 含义 |
|--------|------|
| `process_deploy_ok` | Gateway 日志出现 Process deploy 成功 |
| `mock_llm_called` | Mock LLM 收到 `POST /v1/chat/completions` |
| `mock_llm_responded` | Mock 返回 SSE token（`Streamed token: mock token1`） |
| `chat_result.accepted/completed` | WebChannel 请求被接受且对话结束 |
| `content_len=0` 不出现 | Agent LLM 响应非空（流式解析正确） |
| `jiuwenswarm.db` 存在 | 共享 SQLite 已创建 |
| server workspace `.env` 不含 mock 端口 | Agent 未误读 enterprise 测试配置 |

## logging_config 进程 E2E

`test_logging_config_process_e2e.py::test_logging_config_process_hot_reload_gdb_and_cold_start`

详细五阶段调用链见：[test_logging_config_process_hot_reload_gdb_and_cold_start.md](./test_logging_config_process_hot_reload_gdb_and_cold_start.md)

在真实进程栈（Mock LLM + Claw Manager + Gateway + Process deploy AgentServer）上验证：

1. Manager REST `PUT /logging` → Manager WS `config.push` → Gateway `manager_ws_client` 热更新
2. Gateway DB（由仓库 `.env` 的 `GATEWAY_DB_*` 决定）持久化 `logging_config`
3. WebChannel `chat.send` 拉起 AgentServer 后，Agent 冷启动从 GDB 加载日志级别
4. Gateway 重启后从 GDB 冷加载日志级别
5. `DELETE /logging` 后 GDB 行清除且 Gateway 恢复默认级别

```bash
python -m pytest tests/system_tests/enterprise/test_logging_config_process_e2e.py -v -s -o addopts=""
```

## 如何运行

在 `jiuwenswarm` 仓库根目录：

```bash
# 推荐：跳过 pytest.ini 中的 coverage/timeout 等全局 addopts
python -m pytest tests/system_tests/enterprise/test_gateway_runtime_e2e.py -v -s -o addopts=""
```

依赖：

- 本仓库 `jiuwenswarm` 及 monorepo 中的 `agent-runtime`（foundation + management）
- EE 扩展：`packages/jiuwenclaw-ee/gateway/extensions`
- Python 包：`pytest`、`pytest-asyncio`、`websockets`、`python-dotenv` 等（见根目录 `pyproject.toml`）
- logging_config 用例另需单独安装 Claw Manager，见 [test_logging_config.md](./test_logging_config.md#运行前准备)

## 调试

测试结束后 **不会删除** `.runs/<timestamp>/`，便于排查：

| 日志 / 产物 | 路径 |
|-------------|------|
| Gateway 标准输出 | `.runs/<ts>/gateway/gateway.log` |
| AgentServer 标准输出 | `.runs/<ts>/server/agentserver.log` |
| Agent 内置日志 | `.runs/<ts>/server/.jiuwenswarm/service_default/.logs/` |
| Mock LLM | `.runs/<ts>/mock_llm.log` |
| 共享 DB | `.runs/<ts>/jiuwenswarm.db` |

终端中带 `[E2E][stage]` 前缀的结构化日志可在 `pytest -s` 下直接查看。

## 相关代码

- Gateway EE 扩展：`packages/jiuwenclaw-ee/gateway/extensions/runtime_management_extension/`
- Process 部署：`agent-runtime/management/.../process_service_handler.py`
- Agent 企业配置读库：`jiuwenclaw/agentserver/enterprise_config/loader.py`
- Gateway DB：`packages/jiuwenclaw-ee/gateway/extensions/manager_ws_client/`
