# 示例总览

本目录提供可运行的示例工程，帮助快速理解与接入 AgentSSAS。所有示例：

- **clone 即跑**：不依赖 LLM、不依赖外网（HTTP 示例仅使用 localhost）
- **事件自包含**：合成事件在示例内手写（三层结构 raw_event），不依赖 jiuwenswarm 运行时
- **Python >= 3.11**，需先安装 agent-ssas（见根 [README.zh.md](../README.zh.md) 安装章节）

| 示例 | 演示内容 | 运行入口 |
| ---- | -------- | -------- |
| [inprocess_quickstart/](inprocess_quickstart/) | 进程内模式最小闭环：创建后端 → 上报 7 个合成事件 → 打印 RiskAssessment → 展示落盘数据 | `main.py` |
| [http_server_demo/](http_server_demo/) | HTTP 服务模式：启动独立服务端 → 客户端 POST 上报事件 → 打印风险评估 | `start_server.py` + `client.py` |
| [jiuwenswarm_integration/](jiuwenswarm_integration/) | JiuwenSwarm 集成：应用 patch、安装依赖、配置与启动验证 | patch + jiuwenswarm |

## 快速体验

```bash
# 1. 进程内模式(AgentSSAS 仓库根目录执行)
uv run python examples/inprocess_quickstart/main.py

# 2. HTTP 服务模式(需 http 扩展依赖)
uv run --extra http python examples/http_server_demo/start_server.py
# 另开终端
uv run python examples/http_server_demo/client.py
```

## 事件说明

两个模式示例上报相同的事件流：

- **6 个生命周期事件**：`invoke_start` → `llm_input` → `tool_input` → `tool_output` → `llm_output` → `invoke_end`（模拟一次完整的工具调用交互）
- **1 个安全检测衍生事件**：`permission_interrupt_tool`（模拟其他安全 Rail 拒绝了一次危险工具调用 `rm -rf /`）

生命周期事件预期返回 `risk_level=safe`；安全检测事件由 `security_rail_detection` 模块以 notify 模式后台分析，分析结果异步写入威胁日志与告警表。

更多文档参见 [docs/](../docs/) 目录。
