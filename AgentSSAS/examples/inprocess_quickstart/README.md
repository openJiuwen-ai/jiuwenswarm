# 进程内模式快速上手（inprocess_quickstart）

演示 AgentSSAS 进程内模式的最小闭环：在同一个 Python 进程内创建分析引擎、上报合成事件、获取风险评估并查看落盘数据。零网络延迟，适合单 Agent 场景。

## 运行

在 AgentSSAS 仓库根目录执行：

```bash
uv run python examples/inprocess_quickstart/main.py
```

## 预期输出

```text
[demo] 存储路径: /home/<user>/.jiuwenswarm/ssas
[demo] event=invoke_start               risk_level=safe     has_risk=False
[demo] event=llm_input                   risk_level=safe     has_risk=False
[demo] event=tool_input                  risk_level=safe     has_risk=False
[demo] event=tool_output                 risk_level=safe     has_risk=False
[demo] event=llm_output                  risk_level=safe     has_risk=False
[demo] event=invoke_end                  risk_level=safe     has_risk=False
[demo] event=permission_interrupt_tool   risk_level=safe     has_risk=False
[demo] SQLite 数据库: /home/<user>/.jiuwenswarm/ssas/ssas_core.db (存在)
[demo] OCSF 威胁日志: /home/<user>/.jiuwenswarm/ssas/reports/threat_log/xxx.json
```

说明：

- 生命周期事件无安全语义，返回 `safe`
- `permission_interrupt_tool` 是安全检测事件，`security_rail_detection` 模块以 **notify 模式**订阅：`report_event` 不等待后台检测、立即返回（safe），检测结果异步写入威胁日志与告警表——所以第 7 行仍是 `safe`，但最后一行的威胁日志文件已生成

## 代码要点

1. `AgentSSASConfig.from_dict(None)`：默认配置，存储遵循 `JIUWENSWARM_HOME` 约定（`~/.jiuwenswarm/ssas`），可用环境变量 `SSAS_HOME` 覆盖
2. `AgentSSASBackend(config)` + `await backend.initialize()`：构造后端并加载检测模块
3. `await backend.report_event(raw_event)`：单一接口上报三层结构事件，返回 `RiskAssessment`
4. `await backend.close()`：释放 SQLite 连接（Windows 上必需，否则 WAL 文件锁定目录）

事件格式与字段含义参见 [docs/zh/reference/event-format.md](../../docs/zh/reference/event-format.md)。

## 数据落盘位置

| 数据 | 路径（相对 `<ssas_home>/ssas`） |
| ---- | ------------------------------ |
| 核心库（事件、告警） | `ssas_core.db` |
| OCSF 威胁日志 | `reports/threat_log/*.json` |
| 检测模块结果 | `modules/<module_name>/result.db` |
