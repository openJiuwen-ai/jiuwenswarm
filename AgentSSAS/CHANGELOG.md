# 更新日志 (Changelog)

本项目所有重要变更均记录在本文件中。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本 (SemVer)](https://semver.org/lang/zh-CN/)。

发布入口：<https://atomgit.com/yieux1/AgentSecurity/releases>

## [Unreleased]

## [0.1.0] - 2026-09-04

首个版本。

### 新增

- **双子系统架构**：AgentSSASClient（事件采集与上报客户端，核心类 `AgentSSASSecurityRail` 基于 openJiuwen `BaseSecurityRail` 子类扩展，agent-core 仓零修改）+ AgentSSASCore（威胁检测与态势呈现分析引擎）
- **双运行模式**：进程内模式（inprocess，零网络延迟）与 HTTP 服务模式（多 Agent 共享）
- **事件采集**：6 个生命周期事件 + 1 个安全检测衍生事件，三层消息结构（common / payload / metadata），支持子 Agent 会话（subsession_id）
- **插件化检测引擎**：框架 + 插件架构，检测模块以"建模插件 + 分析插件"对形式注册，通过 `module.yaml` 声明式配置
- **3 个检测模块**：
  - `agent_moss`：Agent 行为态势感知（规则 + 行为链 + PDG 三种分析方法）
  - `security_rail_detection`：其他安全 Rail 检测结果衍生分析
  - `test_detection`：链路验证用模块（默认关闭）
- **notify / auth 双订阅模式**：notify 只观察不干预；auth 参与决策，支持超时兜底策略（allow / deny / default）
- **决策策略**：`observe_only`（仅感知，全部放行）与 `active_protection`（critical 阻断、其余告警），策略定义于 `decision_policies.yaml`
- **fail-open 机制**：SSAS 自身运行故障时返回安全评估并放行，不阻断业务
- **OCSF 威胁日志**：以 OCSF 格式输出威胁日志，支持风险等级过滤
- **SQLite 存储**：事件与告警落盘（遵循 `JIUWENSWARM_HOME` 约定），支持 TTL 清理
- **JiuwenSwarm 集成补丁**：`patches/` 提供本地依赖版（editable）与在线依赖版（git 依赖）两种接入方式，二选一应用
- **测试**：200+ 单元 / 集成 / 端到端测试用例，覆盖进程内与 HTTP 双模式

[Unreleased]: https://atomgit.com/yieux1/AgentSecurity/compare/v0.1.0...develop
[0.1.0]: https://atomgit.com/yieux1/AgentSecurity/releases/tag/v0.1.0
