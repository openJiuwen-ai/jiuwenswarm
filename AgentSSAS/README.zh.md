# AgentSSAS 智能体安全态势感知系统

<p align="center">
  <strong>实时威胁检测 · 事件采集 · 安全决策 —— 面向智能体的通用安全态势感知（当前已接入 JiuwenSwarm）</strong>
</p>

<p align="center">
  <a href="README.md">English</a>
  ·
  <a href="docs/README.md">文档中心</a>
  ·
  <a href="examples/README.md">示例</a>
  ·
  <a href="CHANGELOG.md">更新日志</a>
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

## 定位

智能体（Agent）在执行任务时会调用大模型与外部工具，随之引入新的运行时风险：敏感数据经多步工具调用外泄、危险命令执行、工具滥用、提示注入后潜伏的异常行为链。AgentSSAS 持续采集智能体运行时事件，实时执行威胁检测并输出风险评估，让智能体的安全态势**可见、可查、可决策**。

- **是什么**：通用的智能体安全态势感知系统（Python 包名 `agent-ssas`），提供事件采集、威胁检测、安全决策三层能力，不绑定特定智能体框架
- **解决什么问题**：智能体运行时的威胁检测、行为审计与安全决策辅助
- **与 JiuwenSwarm 的关系**：通过 AgentSSASClient 子系统（核心类 `AgentSSASSecurityRail`，继承 openJiuwen agent-core 的 `BaseSecurityRail` 基类）以插件方式接入 JiuwenSwarm，JiuwenSwarm 主仓只需应用一个小 patch；agent-core 本身零修改
- **可独立使用**：AgentSSASCore 检测引擎不依赖 openjiuwen，任何 Python 智能体框架都可以通过 `AgentSSASBackend` 接口直接嵌入

> **术语说明**：面向 JiuwenSwarm 的完整集成方案——AgentSSASCore 加上 AgentSSASClient 中的 openjiuwen 客户端——在早期文档中也被称为 **JiuwenSSAS**。它只是一个逻辑概念，指代这套组合，并不是一个独立的系统。

## 特性

系统由两个子系统组成：

| 子系统 | 包路径 | 职责 |
| ------ | ------ | ---- |
| **AgentSSASCore** | `agent_ssas.core` | 通用威胁检测引擎：数据预处理、分析流水线、检测模块管理、存储、OCSF 呈现 |
| **AgentSSASClient** | `agent_ssas.backend_client` | 事件采集与上报客户端：提供 openjiuwen 客户端（`agent_ssas.backend_client.openjiuwen`），核心类 `AgentSSASSecurityRail` 继承 agent-core `BaseSecurityRail`，采集 7 个事件（6 个生命周期 + 1 个安全检测衍生）并上报检测引擎 |

核心能力：

- **双运行模式**：进程内模式（`inprocess`，默认，零网络延迟）与 HTTP 服务模式（`http`，独立 FastAPI 服务，多 Agent 共享分析引擎）
- **插件化检测模块**：检测模块通过 `module.yaml` 声明式注册，以"数据建模插件 + 威胁分析插件"成对组织；新增检测能力零侵入框架代码
- **3 个内置检测模块**：`agent_moss`（规则 + 行为链 + PDG 数据泄露分析）、`security_rail_detection`（其他安全 Rail 检测结果的衍生分析）、`test_detection`（链路验证，默认关闭）
- **notify / auth 双订阅模式**：notify 后台异步检测不阻断业务；auth 同步参与安全决策，支持超时兜底策略
- **决策策略**：`observe_only`（默认，仅态势感知全部放行）与 `active_protection`（critical 阻断、其余告警），通过配置一键切换
- **fail-open 设计**：SSAS 自身任何故障（初始化失败、事件非法、检测超时）都不阻断 JiuwenSwarm 主流程
- **OCSF 威胁日志**：检测结果以 OCSF 格式落盘 JSON 日志，事件与告警持久化到 SQLite

**Roadmap**：态势呈现 Web 界面与更多检测模块（插件化架构持续扩展）正在规划中，当前 0.1.x 提供 OCSF 威胁日志与 SQLite 落盘。

## 文档与资源

| 资源 | 说明 |
| ---- | ---- |
| [docs/](docs/README.md) | 双语文档中心：教程、指南、参考、解析四类（zh / en） |
| [examples/](examples/README.md) | 3 个可运行示例：进程内快速上手、HTTP 服务演示、JiuwenSwarm 集成 |
| [design/](design/README.md) | 开发设计稿：整体架构与两个子系统功能设计、测试报告 |
| [patches/](patches/) | JiuwenSwarm 集成补丁：本地依赖版 / 在线依赖版二选一 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录（[Release 入口](https://atomgit.com/yieux1/AgentSecurity/releases)） |
| [SECURITY.md](SECURITY.md) | 安全策略与漏洞报告方式 |

仓库布局：

```
AgentSSAS/
├── README.md / README.zh.md     # 双语自述文件
├── LICENSE / CHANGELOG.md / SECURITY.md
├── docs/                          # 双语四类用户文档（zh / en）
├── examples/                      # 3 个可运行示例
├── design/                        # 开发设计稿与测试报告
├── patches/                       # JiuwenSwarm 集成补丁（local / online）
├── src/agent_ssas/
│   ├── core/                      # AgentSSASCore 子系统（检测引擎）
│   └── backend_client/openjiuwen/ # AgentSSASClient 子系统（事件采集）
└── tests/                         # 200+ 测试用例
```

## 环境要求

- Python >= 3.11
- Windows / Linux / macOS
- HTTP 服务模式需额外安装可选依赖（fastapi / uvicorn，见下文安装）

## 安装

### 远程安装（推荐）

```bash
uv pip install "agent-ssas @ git+https://atomgit.com/yieux1/AgentSecurity.git@develop#subdirectory=AgentSSAS"
```

### 本地安装（开发）

```bash
cd AgentSecurity/AgentSSAS
uv venv
uv pip install -e .

# HTTP 服务模式（可选）
uv pip install -e ".[http]"
```

## Quick Start

### 60 秒体验（无需 JiuwenSwarm）

```bash
uv run python examples/inprocess_quickstart/main.py
```

预期输出（事件逐条上报并返回风险评估）：

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

运行后可在 `~/.jiuwenswarm/ssas/` 查看落盘数据：`ssas_core.db`（原始事件）、`modules/*/result.db`（各检测模块结果）、`reports/threat_log/*.json`（OCSF 威胁日志）。

### 集成 JiuwenSwarm

1. 应用 patch（二选一）：

```bash
cd <jiuwenswarm 仓根目录>
# 本地依赖版（editable，适合与 AgentSSAS 源码协同开发）
git apply <AgentSecurity 仓路径>/AgentSSAS/patches/jiuwenswarm_ssas_local.patch
# 或在线依赖版（atomgit 远程依赖，适合正式使用）
git apply <AgentSecurity 仓路径>/AgentSSAS/patches/jiuwenswarm_ssas_online.patch
```

2. 安装依赖并配置：在 `~/.jiuwenswarm/config/config.yaml` 中添加 `ssas` 段（`enabled: true`）
3. 启动 JiuwenSwarm，日志中出现 `AgentSSASSecurityRail create success, mode=inprocess` 即集成成功
4. 与 Agent 交互后检查 `~/.jiuwenswarm/ssas/` 落盘数据

完整步骤与常见问题见 [集成教程](docs/zh/tutorial/03-integrate-jiuwenswarm.md)。

### 更多示例

| 示例 | 入口 | 说明 |
| ---- | ---- | ---- |
| 进程内快速上手 | [examples/inprocess_quickstart/](examples/inprocess_quickstart/) | 最小闭环：上报事件、查看风险评估与落盘 |
| HTTP 服务演示 | [examples/http_server_demo/](examples/http_server_demo/) | 启动独立服务端 + 客户端上报 |
| JiuwenSwarm 集成 | [examples/jiuwenswarm_integration/](examples/jiuwenswarm_integration/) | patch 应用、配置、启动验证全流程 |

## License 与贡献

本项目基于 [Apache License 2.0](LICENSE) 开源。

欢迎通过以下方式参与贡献：

- 报告缺陷与功能建议：[Issues](https://atomgit.com/yieux1/AgentSecurity/issues)
- 提交代码与文档：[Pull Requests](https://atomgit.com/yieux1/AgentSecurity/pulls)
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 的流程私密报告，请勿公开 Issue
