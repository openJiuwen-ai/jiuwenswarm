# 快速开始

> 本篇带你安装 AgentSSAS 并在进程内模式跑通最小闭环：创建分析引擎、上报事件、获取风险评估、查看落盘数据。全程不需要 JiuwenSwarm、LLM 或外网。适合所有希望快速上手 AgentSSAS 的用户。

## 前置条件

- Python >= 3.11（支持 Windows / Linux / macOS）
- 已安装 [uv](https://docs.astral.sh/uv/)

## 安装

进入仓库的 AgentSSAS 目录，创建虚拟环境并以可编辑方式安装：

```bash
cd AgentSecurity/AgentSSAS
uv venv
uv pip install -e .
```

> 如果不方便使用本地源码，也可以从远程仓库安装：
>
> ```bash
> uv pip install "agent-ssas @ git+https://atomgit.com/yieux1/AgentSecurity.git@develop#subdirectory=AgentSSAS"
> ```

本教程只用到进程内模式，无需额外依赖。HTTP 服务模式需要额外安装 fastapi / uvicorn，参见[HTTP 服务模式](./04-http-mode.md)。

## 运行快速上手示例

在 AgentSSAS 仓库根目录执行：

```bash
uv run python examples/inprocess_quickstart/main.py
```

预期输出如下（存储路径中的用户目录因系统而异，Windows 上形如 `C:\Users\<user>\.jiuwenswarm\ssas`）：

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

### 输出解读

示例构造了 7 个事件的完整事件流：前 6 个生命周期事件模拟一次包含工具调用的交互（`invoke_start` → `llm_input` → `tool_input` → `tool_output` → `llm_output` → `invoke_end`），第 7 个 `permission_interrupt_tool` 模拟其他安全 Rail 拒绝了一次危险工具调用。

- 前 6 个生命周期事件本身不携带安全语义，逐条返回 `risk_level=safe`
- `permission_interrupt_tool` 是安全检测衍生事件，由 `security_rail_detection` 模块以 **notify 模式**订阅：`report_event` 不等待后台检测、立即返回（`safe`），检测结论异步写入威胁日志与告警表——所以第 7 行仍是 `safe`，但最后一行的威胁日志文件已生成
- 示例末尾的 `await asyncio.sleep(0.5)` 就是在等待 notify 模式的后台检测完成

### 示例代码要点

`examples/inprocess_quickstart/main.py` 演示了 AgentSSASCore 的最小用法：

1. `AgentSSASConfig.from_dict(None)`：默认配置，存储遵循 JIUWENSWARM_HOME 约定（`~/.jiuwenswarm/ssas`），可用环境变量 `SSAS_HOME` 覆盖
2. `AgentSSASBackend(config)` 加 `await backend.initialize()`：构造后端并扫描加载检测模块
3. `await backend.report_event(raw_event)`：单一接口上报三层结构（`common` / `payload` / `metadata`）的事件，返回 `RiskAssessment`
4. `await backend.close()`：释放 SQLite 连接（Windows 上必需，否则 WAL 文件会锁定目录）

事件的完整格式与字段含义参见[事件格式参考](../reference/event-format.md)。

## 查看落盘数据

运行结束后，检查 `~/.jiuwenswarm/ssas/` 目录：

| 数据 | 路径（相对 `~/.jiuwenswarm/ssas/`） |
| ---- | ------------------------------------ |
| 核心库（事件、告警） | `ssas_core.db`（SQLite，含 `raw_events` 表） |
| OCSF 威胁日志 | `reports/threat_log/*.json` |
| 检测模块结果 | `modules/<模块名>/result.db` |

例如 `modules/agent_moss/result.db` 与 `modules/security_rail_detection/result.db` 就是两个内置模块各自的检测结果库。

存储根目录的解析优先级为 `SSAS_HOME` > `JIUWENSWARM_DATA_DIR` > `JIUWENSWARM_HOME` > `~/.jiuwenswarm`，最终数据落在 `<根目录>/ssas/`。存储位置相关配置参见[配置参考](../reference/configuration.md)。

## 下一步

- [集成 JiuwenSwarm](./03-integrate-jiuwenswarm.md)：让 JiuwenSwarm 在运行时自动采集 Agent 行为事件
- [HTTP 服务模式](./04-http-mode.md)：把分析引擎部署为独立服务，供多个 Agent 共享
- [编写自定义检测模块](./05-custom-detection-module.md)：实现你自己的检测逻辑
- 完整示例说明：[examples/inprocess_quickstart/README.md](../../../examples/inprocess_quickstart/README.md)
